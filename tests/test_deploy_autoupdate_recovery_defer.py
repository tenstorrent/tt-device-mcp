# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Autoupdate and a broker in recovery (spec 08 I23), and the update decision log (I22).

A restart between two rungs of a hold throws the episode back to the startup check, a forced
fabric pass on a mesh the broker already knows is bad. So while /health reports a hold, or the
FSM is recovering or down, the update waits, bounded: never past the point where the hold itself
is older than the bound, nor past the bound since the update was first deferred. Every decision
is written, with time and both shas, where it outlives a reboot.

The script's sections run for real, cut from it, under stand-ins for curl, logger, systemctl and
the clock. Nothing here touches the host's broker, socket, inhibit file or journal.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"
_OLD = "1" * 40
_NEW = "2" * 40


def _health(held=False, state="healthy", age=None):
    body = {"status": "degraded" if held else "ok", "held": held, "fsm_state": state}
    if age is not None:
        body["held_age_sec"] = age
    return json.dumps(body)


def _top():
    """From the decision log through the recovery gate: everything an update lap decides before
    it touches dispatch."""
    text = _SCRIPT.read_text()
    top = text[text.index("# Decision log (spec 08 I22)") : text.index("# Dispatch hold (spec 08 I18")]
    assert top.count("INHIBIT=/run/tt-device-broker/device-op.lock\n") == 1
    return top


def _tail():
    text = _SCRIPT.read_text()
    return text[text.index("# Last look before the restart") :]


def _run(tmp_path, body, *, health=(), env=None, pending=None, curl_fails=False, stand_ins=""):
    """Run `body` under the stand-ins. `health` is the /health answers in call order (the last one
    repeats)."""
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    logs = tmp_path / "logs"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    py = venv / "bin" / "python"
    if not py.exists():
        py.symlink_to(sys.executable)
    answers = tmp_path / "health.answers"
    answers.write_text("".join(h + "\n" for h in health))
    calls = tmp_path / "health.calls"
    calls.write_text("0")
    trace = tmp_path / "trace"
    if pending is not None:
        (root / "recovery.defer").write_text(pending)
    body = body.replace("INHIBIT=/run/tt-device-broker/device-op.lock\n", f"INHIBIT={tmp_path / 'device-op.lock'}\n")
    prelude = f"""set -u
BR=main; ROOT={root}; VENV={venv}; STATE={root}/installed.sha; src_desc=test
installed_sha={_OLD}; remote_sha={_NEW}
echo {_OLD} > "$STATE"
HOLD={tmp_path}/dispatch-hold
curl() {{
    {"return 22" if curl_fails else ""}
    n=$(cat {calls}); n=$((n+1)); echo "$n" > {calls}
    total=$(wc -l < {answers})
    [ "$n" -le "$total" ] || n=$total
    sed -n "${{n}}p" {answers}
}}
logger() {{ echo "logger $*" >> {trace}; }}
sleep() {{ echo "sleep $*" >> {trace}; }}
systemctl() {{
    case "$*" in
        restart*) echo "restart" >> {trace} ;;
    esac
}}
op_in_flight() {{ return 1; }}
hold_write() {{ :; }}
count_busy() {{ kids=0; holders=0; scopes=0; queued=0; busy=0; }}
{stand_ins}
"""
    env = {"PATH": "/usr/bin:/bin", "TTDEV_LOG_DIR": str(logs), **(env or {})}
    done = subprocess.run(["bash", "-c", prelude + body], capture_output=True, text=True, env=env, timeout=60)
    lines = trace.read_text().splitlines() if trace.exists() else []
    log = logs / "autoupdate.log"
    return done, lines, (log.read_text().splitlines() if log.exists() else [])


def _gate(tmp_path, **kw):
    return _run(tmp_path, _top() + 'echo "GATE PASSED"\n', **kw)


# --- the gate before an update starts -----------------------------------------------------------


def test_a_fresh_hold_defers_the_update(tmp_path):
    done, trace, log = _gate(tmp_path, health=[_health(held=True, state="recovering", age=60)])
    assert done.returncode == 0, done.stderr
    assert "GATE PASSED" not in done.stdout
    assert "broker recovery in progress" in done.stderr
    assert len(log) == 1 and "decision=deferred" in log[0] and "hold_age=60s" in log[0], log
    assert (tmp_path / "root" / "recovery.defer").read_text().split()[0] == _NEW


def test_a_recovering_or_down_fsm_defers_without_a_held_flag(tmp_path):
    for state in ("recovering", "down"):
        sub = tmp_path / state
        sub.mkdir()
        done, _, log = _gate(sub, health=[_health(state=state)])
        assert done.returncode == 0, done.stderr
        assert "GATE PASSED" not in done.stdout, state
        assert f"fsm={state}" in log[0], log


def test_a_healthy_broker_is_not_deferred_for(tmp_path):
    done, _, log = _gate(tmp_path, health=[_health()])
    assert "GATE PASSED" in done.stdout, done.stderr
    assert log == []
    assert not (tmp_path / "root" / "recovery.defer").exists()


def test_a_broker_that_does_not_answer_is_not_deferred_for(tmp_path):
    # An update may be exactly what a wedged broker needs.
    done, _, log = _gate(tmp_path, curl_fails=True)
    assert "GATE PASSED" in done.stdout, done.stderr
    assert log == []


def test_a_hold_older_than_the_bound_lets_the_update_apply(tmp_path):
    # Every fault class's budget is under the bound: a hold past it is over budget, and the update
    # may be its fix.
    done, _, _ = _gate(tmp_path, health=[_health(held=True, state="recovering", age=1200)])
    assert "GATE PASSED" in done.stdout, done.stderr
    assert "past the deferral bound" in done.stderr
    done, _, _ = _gate(tmp_path, health=[_health(held=True, state="recovering", age=1199)])
    assert "GATE PASSED" not in done.stdout


def test_an_update_waits_at_most_the_bound_across_holds(tmp_path):
    """Clocked from the first deferral of a pending update: a hold that keeps coming back, or a
    newer commit, does not restart the clock."""
    first = int(time.time()) - 1201
    done, _, _ = _gate(tmp_path, health=[_health(held=True, age=30)], pending=f"{'3' * 40} {first}\n")
    assert "GATE PASSED" in done.stdout, done.stderr
    # Inside the bound it still waits, and keeps the first deferral's clock for the new sha.
    first = int(time.time()) - 600
    done, _, _ = _gate(tmp_path, health=[_health(held=True, age=30)], pending=f"{'3' * 40} {first}\n")
    assert "GATE PASSED" not in done.stdout
    sha, since = (tmp_path / "root" / "recovery.defer").read_text().split()
    assert sha == _NEW and int(since) == first


def test_the_bound_is_capped_at_30_minutes_and_0_turns_it_off(tmp_path):
    big = {"TTDEV_RECOVERY_DEFER_SEC": "99999"}
    done, _, _ = _gate(tmp_path, health=[_health(held=True, age=1799)], env=big)
    assert "GATE PASSED" not in done.stdout
    done, _, _ = _gate(tmp_path, health=[_health(held=True, age=1800)], env=big)
    assert "GATE PASSED" in done.stdout, done.stderr
    done, _, _ = _gate(tmp_path, health=[_health(held=True, age=5)], env={"TTDEV_RECOVERY_DEFER_SEC": "0"})
    assert "GATE PASSED" in done.stdout, done.stderr


def test_the_default_bound_is_20_minutes():
    assert 'RECOVERY_DEFER="${TTDEV_RECOVERY_DEFER_SEC:-1200}"' in _SCRIPT.read_text()


def test_the_device_op_bar_still_comes_first_and_is_logged(tmp_path):
    # Unconditional, before the recovery gate: no bound lets an update restart over a running op.
    (tmp_path / "device-op.lock").write_text(f"{os.getpid()} reset\n")
    done, _, log = _gate(tmp_path, health=[_health()], env={"TTDEV_RECOVERY_DEFER_SEC": "0"})
    assert "GATE PASSED" not in done.stdout
    assert "device op in flight" in done.stderr
    assert len(log) == 1 and "decision=deferred" in log[0] and "device op in flight (reset)" in log[0]
    text = _SCRIPT.read_text()
    assert text.index("device op in flight") < text.index("if recovery_gate; then") < text.index("# Dispatch hold")


def test_each_deferral_names_its_deadline(tmp_path):
    # The deadline is the bound from the hold's start or from the first deferral, whichever is
    # sooner: a 600 s old hold under a 1200 s bound applies about 600 s from now.
    now = int(time.time())
    done, _, log = _gate(tmp_path, health=[_health(held=True, state="recovering", age=600)])
    assert "GATE PASSED" not in done.stdout
    m = re.search(r"apply_by=([^\s)]+)", log[0])
    assert m, log
    got = subprocess.run(["date", "-d", m.group(1), "+%s"], capture_output=True, text=True, check=True)
    assert abs(int(got.stdout) - (now + 600)) <= 5, (m.group(1), now)
    # An earlier first deferral of this update is the sooner deadline.
    sub = tmp_path / "pending"
    sub.mkdir()
    done, _, log = _gate(sub, health=[_health(held=True, age=60)], pending=f"{_NEW} {now - 1100}\n")
    got = subprocess.run(
        ["date", "-d", re.search(r"apply_by=([^\s)]+)", log[0]).group(1), "+%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(int(got.stdout) - (now + 100)) <= 5, log


_RESET_SCOPE = """systemctl() {
    case "$*" in
        list-units*ttdev-reset-*) echo "ttdev-reset-1234.scope loaded active running reset" ;;
        restart*) echo "restart" >> TRACE ;;
    esac
}
"""


def test_a_running_reset_scope_always_defers_even_past_the_bound(tmp_path):
    """A reset step is bounded, so it is waited out with no bound of the updater's own: a restart
    mid-reset kills the running tt-smi. Holds well past the bound, or no /health at all, change
    nothing."""
    stand_ins = _RESET_SCOPE.replace("TRACE", str(tmp_path / "trace"))
    for name, kw in (
        ("held_past_bound", {"health": [_health(held=True, state="recovering", age=99999)]}),
        ("healthy", {"health": [_health()]}),
        ("no_health", {"curl_fails": True}),
        ("bound_off", {"health": [_health(held=True, age=5)], "env": {"TTDEV_RECOVERY_DEFER_SEC": "0"}}),
    ):
        sub = tmp_path / name
        sub.mkdir()
        done, trace, log = _gate(sub, stand_ins=stand_ins, **kw)
        assert done.returncode == 0, (name, done.stderr)
        assert "GATE PASSED" not in done.stdout, name
        assert "reset scope is still running" in done.stderr, name
        assert len(log) == 1 and "decision=deferred" in log[0] and "device reset scope running" in log[0], log
        assert "apply_by=" in log[0], log
        assert "restart" not in trace, name


def test_a_running_job_on_a_healthy_broker_is_left_to_the_idle_rule(tmp_path):
    """A job is not a recovery: the recovery gate lets the lap through, and the idle gate after it
    (spec 08 I20, I24: drain at once, bounded by the job cap) decides."""
    body = json.loads(_health())
    body.update(running=1, queued=2, total_jobs=3)
    done, _, log = _gate(tmp_path, health=[json.dumps(body)])
    assert "GATE PASSED" in done.stdout, done.stderr
    assert log == []
    assert not (tmp_path / "root" / "recovery.defer").exists()
    text = _SCRIPT.read_text()
    assert text.index("if recovery_gate; then") < text.index("# Idle gate (spec 08 I24)") < text.index("count_busy\nif")
    assert "TTDEV_MAX_DEFER_SEC" not in text


def test_the_deferral_touches_no_recovery_switch():
    """It delays the update's restart only; it never turns off or slows a rung (msg: recovery is
    the broker's, in code, and nothing in the updater weakens it)."""
    text = _SCRIPT.read_text()
    section = text[text.index("# Recovery deferral (spec 08 I23)") : text.index("# Dispatch hold")]
    for switch in ("AUTO_REBOOT", "AUTO_POWER_CYCLE", "AUTO_UBB_RESET", "hold-release", "/hold", "POST", "systemctl"):
        assert switch not in section, switch


# --- the last look before the restart ------------------------------------------------------------


def _waits():
    """The job-boundary wait the last look uses (spec 08 I20, I24), cut from the script. The
    busy count and the device-op probe stay the stand-ins'."""
    text = _SCRIPT.read_text()
    age = text[text.index("youngest_job_age() {") : text.index("# The device-op bar above, asked again")]
    wait = text[text.index("wait_job_boundary() {") : text.index("\ncount_busy\nif")]
    return "JOB_CAP=1560\nUPTIME=/proc/uptime\n" + age + wait + "\n"


def _apply(tmp_path, **kw):
    """The top gate (healthy at first) and the restart tail, with the install in between elided."""
    body = _top() + _waits() + 'echo "autoupdate: applying"\ntrap au_exit EXIT\n' + _tail()
    return _run(tmp_path, body, **kw)


def test_a_clean_apply_restarts_and_logs_it(tmp_path):
    done, trace, log = _apply(tmp_path, health=[_health()])
    assert done.returncode == 0, done.stderr
    assert "restart" in trace
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == _NEW
    assert len(log) == 1 and "decision=applied" in log[0], log


def test_a_hold_that_began_during_the_install_defers_the_restart(tmp_path):
    done, trace, log = _apply(tmp_path, health=[_health(), _health(held=True, state="recovering", age=20)])
    assert done.returncode == 0, done.stderr
    assert "restart" not in trace, "the restart landed in a hold that began during the install"
    # installed.sha stays on the old sha, so the next lap goes through every gate again.
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == _OLD
    assert len(log) == 1 and "decision=deferred" in log[0] and "at restart time" in log[0], log


def test_a_device_op_at_restart_time_is_waited_out(tmp_path):
    # The op ends after three 5 s polls.
    trace = tmp_path / "trace"
    stand_in = f'op_in_flight() {{ [ "$(cat {trace} 2>/dev/null | grep -c "^sleep")" -lt 3 ]; }}'
    done, trace, log = _apply(tmp_path, health=[_health()], stand_ins=stand_in)
    assert done.returncode == 0, done.stderr
    steps = [t for t in trace if not t.startswith("logger ")]
    assert steps == ["sleep 5"] * 3 + ["restart"], trace
    assert "decision=applied" in log[-1]


def test_a_device_op_that_outlives_the_wait_is_never_restarted_over(tmp_path):
    clock = tmp_path / "clock"
    clock.write_text(str(int(time.time())))
    stand_in = (
        "op_in_flight() { return 0; }\n"
        f'date() {{ if [ "$*" = "+%s" ]; then n=$(cat {clock}); echo $((n+100)) > {clock}; echo "$n"; '
        'else command date "$@"; fi; }'
    )
    done, trace, log = _apply(tmp_path, health=[_health()], stand_ins=stand_in)
    assert done.returncode == 0, done.stderr
    assert "restart" not in trace
    assert (tmp_path / "root" / "installed.sha").read_text().strip() == _OLD
    assert "decision=deferred" in log[-1] and "device op still in flight at restart time" in log[-1]
    assert "job cap 1560s" in log[-1], log


def test_the_restart_still_comes_after_the_last_look():
    tail = _tail()
    assert tail.index('wait_job_boundary "restart point"') < tail.index("if recovery_gate") < tail.index('> "$STATE"')
    assert tail.index('> "$STATE"') < tail.index("systemctl restart tt-device-broker")


# --- the decision log (spec 08 I22) --------------------------------------------------------------


def test_each_decision_line_has_time_both_shas_and_a_reason(tmp_path):
    done, trace, log = _gate(tmp_path, health=[_health(held=True, age=60)])
    line = log[0]
    m = re.match(
        r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}) decision=deferred branch=main old=(\w+) new=(\w+) reason=(.+)$",
        line,
    )
    assert m, line
    assert m.group(2) == _OLD and m.group(3) == _NEW and "broker recovery in progress" in m.group(4)
    # The same line goes to the journal under a stable tag.
    assert any(t.startswith("logger -t tt-device-autoupdate -- ") and t.endswith(line) for t in trace), trace


def test_an_apply_that_stops_is_logged_as_failed(tmp_path):
    body = _top() + 'echo "autoupdate: applying"\ntrap au_exit EXIT\nexit 1\n'
    done, _, log = _run(tmp_path, body, health=[_health()])
    assert done.returncode == 1
    assert len(log) == 1 and "decision=failed" in log[0] and "exit 1" in log[0], log


def test_the_log_is_kept_to_about_a_mebibyte(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "autoupdate.log").write_text("x" * 1048576)
    done, _, log = _gate(tmp_path, health=[_health(held=True, age=60)])
    assert len(log) == 1 and "decision=deferred" in log[0]
    assert (logs / "autoupdate.log.1").stat().st_size == 1048576


def test_the_log_lives_in_the_broker_log_dir():
    assert 'AU_LOG="${TTDEV_LOG_DIR:-/var/log/tt-device-broker}/autoupdate.log"' in _SCRIPT.read_text()
