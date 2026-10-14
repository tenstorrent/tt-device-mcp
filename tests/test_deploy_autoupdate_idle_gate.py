# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The auto-update's two busy gates, which are not the same gate.

A running JOB must not hold an update back: it lives in its own scope and is re-adopted
across the restart (test_readopt), so waiting for idle buys no safety and leaves a host
stale on the fix it is waiting for. An in-flight DEVICE OP must: re-adoption covers a job,
not a tt-smi that KillMode=control-group SIGTERMs mid-reset, which is a half-reset mesh and
a chassis power cycle.
"""

import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"


def test_the_idle_gate_is_off_by_default():
    assert 'MAX_DEFER="${TTDEV_MAX_DEFER_SEC:-0}"' in _SCRIPT.read_text()


def test_a_host_can_still_ask_for_an_idle_window():
    # The bounded wait stays reachable for a host that sets the knob; only the default moved.
    text = _SCRIPT.read_text()
    assert '[ "$waited" -lt "$MAX_DEFER" ]' in text
    assert '[ "$MAX_DEFER" -gt 0 ]' in text


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


def _gate(tmp_path, *, scopes="", queued=0, max_defer=600, stand_ins=""):
    text = _SCRIPT.read_text()
    start = text.index("# Dispatch hold (spec 08 I18")
    end = text.index('echo "autoupdate: applying', start)
    gate = text[start:end]
    hold = tmp_path / "run" / "dispatch-hold"
    hold.parent.mkdir()
    assert gate.count("HOLD=/run/tt-device-broker/dispatch-hold\n") == 1
    gate = gate.replace("HOLD=/run/tt-device-broker/dispatch-hold\n", f"HOLD={hold}\n")
    trace = tmp_path / "trace"
    logs = tmp_path / "logs"
    (logs / "queued").mkdir(parents=True)
    for i in range(queued):
        (logs / "queued" / f"{i}.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    prelude = f"""set -u
ROOT={root}; remote_sha=abc123
seen() {{ if [ -e "$HOLD" ]; then echo "$1 held $(cat "$HOLD")" >> {trace}; else echo "$1 free" >> {trace}; fi; }}
sleep() {{ seen "sleep $*"; }}
pgrep() {{ seen pgrep; }}
ls() {{ :; }}  # no real /proc holders
systemctl() {{
    case "$*" in
        *list-units*ttdev-job-*) seen scopes; [ -n "{scopes}" ] && echo "{scopes}" ;;
        show*) echo 4242 ;;
        restart*) seen restart ;;
    esac
}}
{stand_ins}
"""
    script = prelude + gate + 'echo "autoupdate: applying"\nsystemctl restart tt-device-broker\n'
    env = {
        "PATH": "/usr/bin:/bin",
        "TTDEV_MAX_DEFER_SEC": str(max_defer),
        "TTDEV_HOLD_SETTLE_SEC": "7",
        "TTDEV_LOG_DIR": str(logs),
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
