# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 08 I29: an update that changes no file the broker uses moves the tree and installed.sha
without restarting the broker.

Every restart reruns the full startup check, fabric pass included, so a push that changes only
docs, tests or CI config is a restart for nothing. The allowlist is explicit and fail-safe: any
other path, a diff that cannot be computed, a clone not at the installed sha or a broker that is
not healthy restarts as before. The rate limit (I21) and the idle gate (I24) still gate the lap.

Each lap below runs the script's own lines, from the installed-sha read to its end, against a real
git clone in tmp_path, with systemd, pip, the staging `install` and the host-config apply stubbed
to record what ran.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.deploy_helpers import LAP_STAND_INS

# Fork-heavy deploy-script laps: one xdist worker runs them all (pyproject: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"
_HEALTHY = '{"status":"ok","held":false,"fsm_state":"healthy","fsm_why":null}'
_HELD = '{"status":"ok","held":true,"held_reason":"fabric check failed","fsm_state":"recovering","fsm_why":"x"}'
_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _clock(text):
    """The script's clock seam (au_now, au_time) as it defines them: the lap reads the real clock."""
    i = text.index("au_now() {")
    return text[i : text.index("\n", text.index("au_time() {", i)) + 1]


def _git(repo, *args):
    env = {**_GIT_ENV, "HOME": str(repo.parent)}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    ).stdout.strip()


def _commit(repo, files):
    for rel, body in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        if rel.endswith(".sh"):
            f.chmod(0o755)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


_BASE = {
    "README.md": "readme\n",
    "pyproject.toml": "[project]\nname='x'\n",
    "src/tt_device_mcp/server.py": "x = 1\n",
    "src/tt_device_mcp/health/gate.py": "g = 1\n",
    "deploy/apply-host-config.sh": "#!/bin/sh\n",
    "deploy/tt-device-fabric-check.sh": "#!/bin/sh\n",
    "deploy/fabric-validator.pin": "PIN=1\n",
    "specs/08-deployment.md": "spec\n",
    "tests/test_x.py": "def test(): pass\n",
    ".github/workflows/ci.yml": "on: push\n",
}


@pytest.fixture
def box(tmp_path):
    """A clone at the installed sha, and the box state around it."""
    clone = tmp_path / "root" / "src"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    installed = _commit(clone, _BASE)
    (tmp_path / "root" / "installed.sha").write_text(installed + "\n")
    eth = tmp_path / "root" / "eth-venv" / "bin" / "python"
    eth.parent.mkdir(parents=True)
    eth.write_text("#!/bin/sh\nexit 0\n")
    eth.chmod(0o755)
    pip = tmp_path / "venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text(f"#!/bin/sh\necho pip >> {tmp_path / 'trace'}\n")
    pip.chmod(0o755)
    (tmp_path / "logs" / "queued").mkdir(parents=True)
    (tmp_path / "run").mkdir()
    return {"tmp": tmp_path, "clone": clone, "installed": installed}


def _lap(box, sha, *, now=10_000, health=_HEALTHY, job_polls=0, stand_ins=""):
    """One lap that has just resolved `sha`. `job_polls`: a running job scope that ends after that
    many drain polls. Returns (completed process, trace lines, events)."""
    tmp = box["tmp"]
    root = tmp / "root"
    trace = tmp / "trace"
    polls = tmp / "polls"
    text = _SCRIPT.read_text()
    body = text[text.index("# Read as $(cat) would") :]
    for old, new in (
        ("INHIBIT=/run/tt-device-broker/device-op.lock\n", f"INHIBIT={tmp / 'device-op.lock'}\n"),
        ("HOLD=/run/tt-device-broker/dispatch-hold\n", f"HOLD={tmp / 'run' / 'dispatch-hold'}\n"),
        ("for p in /proc/[0-9]*; do", f"for p in {tmp}/proc/[0-9]*; do"),
    ):
        assert body.count(old) == 1, old
        body = body.replace(old, new)
    prelude = f"""set -u
ROOT={root}; CLONE={root}/src; clone={root}/src; STATE={root}/installed.sha; VENV={tmp}/venv
BR=main; src_desc=test; TT_DEVICE_MCP_HEALTH_DIR={root}
remote_sha={sha}
log() {{ echo "$* $(cat "$STATE")" >> {trace}; }}
date() {{ echo {now}; }}
{_clock(text)}au_now() {{ printf -v "$1" %s {now}; }}
chmod() {{ :; }}
ls() {{ :; }}
pgrep() {{ :; }}
install() {{ log "stage $3"; }}
bash() {{ log "host-config"; }}
sleep() {{ log "sleep"; echo x >> {polls}; }}
job_running() {{ [ "$(cat {polls} 2>/dev/null | wc -l)" -lt {job_polls} ]; }}
systemctl() {{
    case "$*" in
        restart*) log restart ;;
        is-active*) echo active ;;
        show*NRestarts*) echo 0 ;;
        show*MainPID*) echo 4242 ;;
        show*ActiveEnterTimestampMonotonic*) echo "" ;;
        *list-units*ttdev-job-*) job_running && echo "ttdev-job-1.scope loaded active running" ;;
    esac
}}
curl() {{ printf '%s' '{health}'; }}
{LAP_STAND_INS}{stand_ins}
"""
    run_env = {
        **_GIT_ENV,
        "HOME": str(tmp),
        "TTDEV_HOLD_SETTLE_SEC": "0",
        "TTDEV_APPLY_QUIET_SEC": "0",
        "TTDEV_LOG_DIR": str(tmp / "logs"),
    }
    done = subprocess.run(["bash", "-c", prelude + body], capture_output=True, text=True, env=run_env, timeout=60)
    lines = trace.read_text().splitlines() if trace.exists() else []
    trace.unlink(missing_ok=True)
    events_file = root / "health_events.jsonl"
    events = [json.loads(x) for x in events_file.read_text().splitlines()] if events_file.exists() else []
    return done, lines, events


def _installed(box):
    return (box["tmp"] / "root" / "installed.sha").read_text().strip()


def _head(box):
    return _git(box["clone"], "rev-parse", "HEAD")


def _restarts(lines):
    return [x for x in lines if x.startswith("restart")]


def _push(box, files):
    """A new commit on the branch, the clone left at the installed sha, as a fetch leaves it."""
    sha = _commit(box["clone"], files)
    _git(box["clone"], "reset", "-q", "--hard", box["installed"])
    return sha


def test_a_docs_tests_and_ci_only_update_moves_the_tree_and_installed_sha_with_no_restart(box):
    new = _push(
        box,
        {
            "README.md": "readme 2\n",
            "specs/08-deployment.md": "spec 2\n",
            "deploy/README.md": "deploy docs\n",
            "docs/guide.md": "guide\n",
            "tests/test_x.py": "def test(): assert 1\n",
            ".github/workflows/ci.yml": "on: [push]\n",
        },
    )
    done, lines, events = _lap(box, new)
    assert done.returncode == 0, done.stderr
    assert _restarts(lines) == [], lines
    # Nothing installed, staged or applied: none of it changed (the one sleep is the hold's settle).
    assert lines == [f"sleep {box['installed']}"], lines
    assert _installed(box) == new == _head(box)
    assert "broker not restarted" in done.stderr, done.stderr
    assert not (box["tmp"] / "root" / "last-apply").exists(), "a no-restart apply moved the rate limit"
    # The decision log (I22) records it as an apply, with both shas and no restart.
    (logged,) = (box["tmp"] / "logs" / "autoupdate.log").read_text().splitlines()
    assert "decision=applied" in logged and "no restart" in logged, logged
    assert f"old={box['installed']}" in logged and f"new={new}" in logged, logged
    (ev,) = events
    assert ev["kind"] == "autoupdate_applied_no_restart"
    assert ev["from_sha"] == box["installed"] and ev["to_sha"] == new
    assert sorted(ev["changed_paths"]) == sorted(
        [
            ".github/workflows/ci.yml",
            "README.md",
            "deploy/README.md",
            "docs/guide.md",
            "specs/08-deployment.md",
            "tests/test_x.py",
        ]
    )
    # The next lap finds it current and does nothing.
    done, lines, events = _lap(box, new)
    assert done.returncode == 0 and lines == [] and len(events) == 1, (done.stderr, lines)


def test_a_mixed_update_restarts(box):
    new = _push(box, {"README.md": "readme 2\n", "src/tt_device_mcp/server.py": "x = 2\n"})
    done, lines, events = _lap(box, new)
    assert done.returncode == 0, done.stderr
    assert "restart needed: changes src/tt_device_mcp/server.py" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines
    assert "pip" in lines and f"host-config {box['installed']}" in lines, lines
    assert _installed(box) == new == _head(box)
    assert [e["kind"] for e in events] == ["autoupdate_apply"]


@pytest.mark.parametrize(
    "path",
    [
        "src/tt_device_mcp/health/gate.py",  # health check
        "src/tt_device_mcp/server.py",  # package code
        "src/tt_device_mcp/notes.md",  # package data: never by extension
        "deploy/tt-device-fabric-check.sh",  # staged script the fabric check runs
        "deploy/fabric-validator.pin",  # validator pin
        "deploy/tt-device-broker.service",  # systemd unit, new file
        "pyproject.toml",  # dependencies
        "install.sh",  # unknown top-level file
    ],
)
def test_an_update_touching_anything_the_broker_uses_restarts(box, path):
    new = _push(box, {"docs/guide.md": "g\n", path: "changed\n"})
    done, lines, events = _lap(box, new)
    assert done.returncode == 0, done.stderr
    assert f"restart needed: changes {path}" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines
    assert "host-config" in " ".join(lines)
    assert [e["kind"] for e in events] == ["autoupdate_apply"]


def test_a_diff_that_cannot_be_computed_restarts(box):
    new = _push(box, {"docs/guide.md": "g\n"})
    fail_diff = 'git() { case " $* " in *" diff "*) return 128 ;; esac; command git "$@"; }'
    done, lines, _ = _lap(box, new, stand_ins=fail_diff)
    assert done.returncode == 0, done.stderr
    assert "restart needed: cannot diff" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines


@pytest.mark.parametrize("installed", ["", "not-a-sha", "--output=/tmp/x"])
def test_an_unreadable_installed_sha_restarts(box, installed):
    new = _push(box, {"docs/guide.md": "g\n"})
    (box["tmp"] / "root" / "installed.sha").write_text(installed + "\n" if installed else "")
    done, lines, _ = _lap(box, new)
    assert done.returncode == 0, done.stderr
    assert "is not a commit" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines


def test_a_clone_left_ahead_by_an_unfinished_apply_restarts(box):
    # An earlier lap reset the clone to `mid` (a code change) and stopped before installed.sha: the
    # venv may already hold `mid`'s code. A docs-only step on top of it must not skip the restart.
    mid = _commit(box["clone"], {"src/tt_device_mcp/server.py": "x = 3\n"})
    new = _commit(box["clone"], {"docs/guide.md": "g\n"})
    _git(box["clone"], "reset", "-q", "--hard", mid)
    done, lines, _ = _lap(box, new)
    assert done.returncode == 0, done.stderr
    assert "restart needed: the clone is at" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines


def test_a_broker_that_is_not_healthy_still_gets_its_restart(box):
    new = _push(box, {"docs/guide.md": "g\n"})
    done, lines, _ = _lap(box, new, health=_HELD)
    assert done.returncode == 0, done.stderr
    assert "restart needed: the broker holds the device" in done.stderr, done.stderr
    assert _restarts(lines) == [f"restart {new}"], lines


def test_the_rate_limit_still_gates_a_no_restart_apply(box):
    new = _push(box, {"docs/guide.md": "g\n"})
    (box["tmp"] / "root" / "last-apply").write_text("10000\n")
    done, lines, events = _lap(box, new, now=10_060)
    assert done.returncode == 0, done.stderr
    assert "rate-limited" in done.stderr and "(min 1800s)" in done.stderr, done.stderr
    assert _installed(box) == box["installed"] == _head(box) and events == [] and lines == []
    done, lines, events = _lap(box, new, now=11_800)
    assert done.returncode == 0, done.stderr
    assert _installed(box) == new == _head(box) and _restarts(lines) == []
    assert [e["kind"] for e in events] == ["autoupdate_applied_no_restart"]
    assert (box["tmp"] / "root" / "last-apply").read_text().strip() == "10000"


def test_the_idle_gate_still_drains_before_a_no_restart_apply(box):
    new = _push(box, {"docs/guide.md": "g\n"})
    done, lines, events = _lap(box, new, job_polls=3)
    assert done.returncode == 0, done.stderr
    assert "draining" in done.stderr and "drained after" in done.stderr, done.stderr
    # The hold's settle and every poll of the drain ran with the old sha still installed; the move
    # came after them.
    polls = [x for x in lines if x.startswith("sleep")]
    assert len(polls) == 3 and all(x == f"sleep {box['installed']}" for x in polls), lines
    assert _installed(box) == new and _restarts(lines) == []
    (ev,) = events
    assert ev["kind"] == "autoupdate_applied_no_restart" and ev["idle"] is False


def test_the_allowlist_is_the_one_the_spec_names():
    text = _SCRIPT.read_text()
    assert "docs/*|tests/*|.github/*|*.md) ;;" in text
    i = text.index("src/*) echo")
    assert i < text.index("docs/*|tests/*|.github/*|*.md) ;;"), "src/ must be refused before *.md matches"


def test_a_no_restart_apply_clears_the_recovery_deferral_clock(box):
    """Spec 08 I23: $ROOT/recovery.defer clocks an update not yet applied. A no-restart apply
    clears it like a restart does, so a later update in a fresh hold is deferred from its own
    first deferral, not let through on the stale clock of one already applied."""
    (box["tmp"] / "venv" / "bin" / "python").symlink_to(sys.executable)  # broker_hold parses /health
    marker = box["tmp"] / "root" / "recovery.defer"
    log = box["tmp"] / "logs" / "autoupdate.log"

    def held(age):
        return '{"status":"degraded","held":true,"fsm_state":"recovering","held_age_sec":%d}' % age

    docs = _push(box, {"docs/guide.md": "g\n"})
    done, lines, _ = _lap(box, docs, now=10_000, health=held(30))
    assert done.returncode == 0 and "deferring" in done.stderr, done.stderr
    assert marker.exists() and _installed(box) == box["installed"] and lines == []

    done, lines, _ = _lap(box, docs, now=10_300)
    assert done.returncode == 0 and "broker not restarted" in done.stderr, done.stderr
    assert _installed(box) == docs and _restarts(lines) == []
    assert not marker.exists(), "the applied update's deferral clock outlived it"

    box["installed"] = docs
    code = _push(box, {"src/tt_device_mcp/server.py": "x = 2\n"})
    done, lines, _ = _lap(box, code, now=200_000, health=held(30))
    assert done.returncode == 0, done.stderr
    assert _restarts(lines) == [] and _installed(box) == docs, (done.stderr, lines)
    assert "decision=deferred" in log.read_text().splitlines()[-1], log.read_text()
    assert marker.read_text().split() == [code, "200000"]
