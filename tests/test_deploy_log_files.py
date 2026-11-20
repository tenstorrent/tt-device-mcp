# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The deployed logging surface (spec 08, "Logging surface" row; spec 06 I9/I10 as deployed):
the logrotate stanza, reconcile's job-log sweep, the installer's /health hint, the validator
build's transcript, and the unit files' modes and streams.

Everything here is asserted on file contents and statements through `deploy_helpers`; no script
is executed and no host path is touched.
"""

import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import JOB_LOG_NAME_NEAR_MISSES
from tests.deploy_helpers import _GALAXY, _one, _render, _statements
from tt_device_mcp.constants import JOB_LOG_NAME_RE, SYSTEM_HEALTH_DIR, SYSTEM_LOG_DIR, SYSTEM_STATE_DIR
from tt_device_mcp.health.recovery import base as recovery_base

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_LOGROTATE = _DEPLOY / "tt-device-broker.logrotate"
_RECONCILE = _DEPLOY / "tt-device-reconcile.sh"
_INSTALLER = _DEPLOY / "install-tt-device-broker.sh"
_VALIDATOR = _DEPLOY / "install-fabric-validator.sh"
_AUTOUPDATE = _DEPLOY / "tt-device-autoupdate.sh"
_CRASH_INSTALLER = _DEPLOY / "install-crash-recorder.sh"
_CRASH_RECORDER = _DEPLOY / "tt-crash-recorder.py"
_APPLY = _DEPLOY / "apply-host-config.sh"
_HELPER = _DEPLOY / "tt-device-log.sh"
_DEVICE_LOCK = _DEPLOY / "tt-device-lock.sh"
_BUSLOCK_UNIT = _DEPLOY / "tt-device-buslock.service"
_UNIT_FILES = sorted(_DEPLOY.glob("*.service"))

_LOG_DIR = "/var/log/tt-device-broker"
_UNIT_LOGS = (
    "server.log",
    "health-probe.log",
    "reconcile.log",
    "autoupdate.log",
    "fabric-validator.log",
    "apply-host-config.log",
    "device-lock.log",
)

# Spec 01's two exact patterns: the job-log name — the Python readers' own constant, so the sweep
# and the readers cannot drift apart — and the same stem with a rotation suffix.
_JOB_LOG = JOB_LOG_NAME_RE.pattern
_JOB_LOG_GZ = _JOB_LOG[: -len(r"\.log$")] + r"\.log\.[0-9]+\.gz$"
assert _JOB_LOG_GZ == r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}_([0-9A-Za-z-]+)\.log\.[0-9]+\.gz$"


# --- logrotate ---------------------------------------------------------------------------------


def _stanzas(text: str) -> list[tuple[str, str]]:
    """(header, body) for each `paths { ... }` stanza, comments stripped."""
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    return re.findall(r"([^{}]+?)\s*\{([^}]*)\}", code)


def test_logrotate_has_one_stanza_naming_every_process_log_and_no_job_log():
    stanzas = _stanzas(_LOGROTATE.read_text())
    assert len(stanzas) == 1, "one stanza: every process log shares one rotation rule"
    header, _ = stanzas[0]
    paths = header.split()
    assert sorted(paths) == sorted(f"{_LOG_DIR}/{n}" for n in _UNIT_LOGS)
    for p in paths:
        assert "*" not in p and "?" not in p, f"a glob could match a job log: {p}"
        assert "_" not in Path(p).name, "a unit log's name has no `_`, so it never matches the job-log pattern"


def test_logrotate_rotates_by_rename_with_create_0644_and_no_copytruncate():
    # `create 0644 root root` states the new file's mode (spec 06 I9); copytruncate would drop what is
    # written between its copy and its truncate, and the broker's WatchedFileHandler plus the
    # deploy scripts' open-per-line make a rename lossless on both sides (spec 08).
    _, body = _stanzas(_LOGROTATE.read_text())[0]
    directives = [ln.strip() for ln in body.splitlines() if ln.strip()]
    assert "create 0644 root root" in directives
    assert "copytruncate" not in directives
    code = "\n".join(ln for ln in _LOGROTATE.read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert "copytruncate" not in code


# --- reconcile's sweep -------------------------------------------------------------------------


def _sweep_statements() -> list[str]:
    return [s for s in _statements(_RECONCILE) if "-delete" in s and not s.lstrip().startswith("#")]


def _pattern_of(stmt: str) -> str:
    m = re.search(r"-regex '([^']+)'", stmt)
    assert m, f"no -regex in {stmt!r}"
    pat = m.group(1)
    assert pat.startswith(
        ".*/"
    ), "GNU find's -regex matches the whole path, so the basename pattern needs the `.*/` prefix"
    return "^" + pat[len(".*/") :] + "$"


def test_reconcile_sweep_is_exactly_the_two_spec_01_patterns_each_aged_30_days_and_nothing_else():
    sweeps = _sweep_statements()
    assert len(sweeps) == 2, f"exactly two -delete statements, got {sweeps}"
    for s in sweeps:
        assert "-regextype posix-extended" in s and "-regex '" in s, s
        assert "-mtime +30" in s, s
        assert "-maxdepth 1 -type f" in s, s
        assert "-name" not in s, "the sweep selects by the exact pattern, never by a `-name` glob"
    assert sorted(_pattern_of(s) for s in sweeps) == sorted([_JOB_LOG, _JOB_LOG_GZ])
    # Nothing else in reconcile deletes a file: its other `rm`s are its own markers under /run (the
    # stray-pid memo and the in-progress state).
    others = [s for s in _statements(_RECONCILE) if re.search(r"\brm\b", s) and not s.lstrip().startswith("#")]
    assert all(
        '"$seen"' in s or '"$inprog"' in s for s in others
    ), f"reconcile deletes only through the two sweeps: {others}"


@pytest.mark.parametrize(
    "name, is_job, is_rotated_job",
    [
        ("2026-01-01_120000_7.log", True, False),
        ("2026-01-01_120000_ubb-reset-3.log", True, False),
        ("2026-01-01_120000_7.log.1.gz", False, True),
        ("server.log", False, False),
        ("reconcile.log", False, False),
        ("autoupdate.log", False, False),
        ("fabric-validator.log", False, False),
        ("apply-host-config.log", False, False),
        ("device-lock.log", False, False),
        ("health-probe.log", False, False),
        ("server.log.1.gz", False, False),
        ("foo_bar.log", False, False),
        ("foo_123.log", False, False),
        ("2026-01-01_120000_7.log.bak", False, False),
        ("x_2026-01-01_120000_7.log", False, False),
        ("2026-01-01_120000_7.log.gz", False, False),
    ],
)
def test_reconcile_sweep_patterns_match_job_logs_and_never_a_unit_log_or_a_near_miss(name, is_job, is_rotated_job):
    # The patterns as written in the script, evaluated over the basename as GNU find would over the
    # path: server.log, the deploy-script logs and spec 01's underscore-bearing near-misses are
    # never the sweep's to delete.
    pats = {_pattern_of(s) for s in _sweep_statements()}
    job = next(p for p in pats if p == _JOB_LOG)
    gz = next(p for p in pats if p == _JOB_LOG_GZ)
    assert bool(re.fullmatch(job, name)) is is_job
    assert bool(re.fullmatch(gz, name)) is is_rotated_job


# --- the installer's I3 hint -------------------------------------------------------------------


def test_installer_health_hint_names_server_log_before_journalctl():
    # The file is what a user without journal access can read (spec 08 I3, spec 06 I10); the journal
    # is the second copy. The `journalctl` text stays, so the existing I3 anchors keep holding.
    stmt = _one(_INSTALLER, "journalctl -u tt-device-broker")
    assert f"{_LOG_DIR}/server.log" in stmt
    assert stmt.index(f"{_LOG_DIR}/server.log") < stmt.index("journalctl -u tt-device-broker")
    assert "exit 1" in stmt


# --- the validator build's transcript -----------------------------------------------------------


def test_validator_build_tees_both_cmake_steps_to_build_log_with_stderr_kept():
    stmts = [s for s in _statements(_VALIDATOR) if not s.lstrip().startswith("#")]
    cmake = [s for s in stmts if re.match(r"\s*cmake\s", s)]
    assert len(cmake) == 2, f"one configure and one build, got {cmake}"
    for s in cmake:
        assert '2>&1 | tee -a "$BUILD/build.log" >&2' in s, s
    assert any(
        re.match(r"\s*set -euo pipefail", s) or re.match(r"\s*set -o pipefail", s) for s in stmts
    ), "pipefail keeps a failing cmake the pipeline's failure, so tee cannot mask it"


def test_validator_build_names_the_transcript_path_in_a_log_line():
    stmt = _one(_VALIDATOR, 'log "building validator', "$BUILD/build.log")
    assert stmt.lstrip().startswith("log ")


def test_validator_build_creates_the_transcript_at_a_stated_mode():
    # 0644 by the script's own hand (spec 06 I10: "created by the script"); the unit's UMask is the backstop.
    _one(_VALIDATOR, "install -m 0644 /dev/null", '"$BUILD/build.log"')


def test_validator_build_starts_each_attempt_fresh_and_keeps_only_the_previous_transcript(tmp_path):
    # Spec 08: the timer retries a failing pin hourly, and an appended transcript would grow without
    # bound. Each attempt starts an empty 0644 build.log; the one before survives as build.log.prev.
    rotate = _one(_VALIDATOR, "mv -f", '"$BUILD/build.log.prev"')
    create = _one(_VALIDATOR, "install -m 0644 /dev/null", '"$BUILD/build.log"')
    stmts = _statements(_VALIDATOR)
    first_cmake = next(i for i, s in enumerate(stmts) if re.match(r"\s*cmake\s", s))
    assert stmts.index(rotate) < stmts.index(create) < first_cmake, "rotate, then create, then build"
    # The transcript lives in the slot being built, which a first build of that slot recreates
    # from nothing: rotate and create only once the slot exists, or the recreate drops both.
    slot_made = _one(_VALIDATOR, 'mkdir -p "$BUILD"')
    assert stmts.index(slot_made) < stmts.index(rotate)

    log, prev = tmp_path / "build.log", tmp_path / "build.log.prev"
    for attempt in ("first", "second", "third"):
        r = subprocess.run(
            ["bash", "-euc", f'umask 077\n{rotate}\n{create}\necho {attempt} >> "$BUILD/build.log"'],
            env={"PATH": "/usr/bin:/bin", "BUILD": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
    assert log.read_text() == "third\n" and stat.S_IMODE(log.stat().st_mode) == 0o644
    assert prev.read_text() == "second\n", "only the previous attempt survives"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["build.log", "build.log.prev"]


# --- unit files ----------------------------------------------------------------------------------


def _directives(text: str) -> list[str]:
    """Unit directives, backslash-continued lines joined (buslock's ExecStart spans two)."""
    out, buf = [], ""
    for ln in text.splitlines():
        if ln.lstrip().startswith("#") and not buf:
            continue
        if ln.rstrip().endswith("\\"):
            buf += ln.rstrip()[:-1] + " "
            continue
        buf += ln
        if buf.strip():
            out.append(buf.strip())
        buf = ""
    return out


@pytest.mark.parametrize(
    "unit",
    [
        "tt-device-fabric-validator.service",
        "tt-device-buslock.service",
        "tt-crash-recorder.service",
        "tt-device-health-probe.service",
        "tt-device-reconcile.service",
    ],
)
def test_the_writer_units_outside_the_broker_state_umask_0022(unit):
    # These write files on their own (build.log, buslock.log, the crash record, the probe's state,
    # autoupdate's and device-lock's health-journal appends) outside the broker's process, so each
    # unit file states the mode itself as a backstop (spec 06 I9, spec 08).
    assert "UMask=0022" in _directives((_DEPLOY / unit).read_text())


def test_no_shipped_or_rendered_unit_sets_standard_output_or_standard_error(tmp_path):
    # stdout/stderr go to the journal by systemd's default, a deliberate second copy; nothing is
    # redirected at the unit level (spec 06 I10).
    assert _UNIT_FILES, "no unit files found under deploy/"
    for unit in _UNIT_FILES:
        for d in _directives(unit.read_text()):
            assert not d.startswith(("StandardOutput=", "StandardError=")), f"{unit.name}: {d}"
    rendered = _render(tmp_path, _GALAXY)
    for d in _directives(rendered):
        assert not d.startswith(("StandardOutput=", "StandardError=")), f"rendered unit: {d}"


def test_no_shipped_or_rendered_unit_runs_a_pipeline_as_its_exec_start(tmp_path):
    # A pipeline at the unit level makes the shell, not the program, the main pid; a redirect
    # (buslock's `>> buslock.log`) is allowed, a `|` or a `tee` is not (spec 06 I10).
    execs = []
    for unit in _UNIT_FILES:
        execs += [(unit.name, d) for d in _directives(unit.read_text()) if d.startswith("ExecStart=")]
    execs += [("rendered", d) for d in _directives(_render(tmp_path, _GALAXY)) if d.startswith("ExecStart=")]
    assert execs
    for name, d in execs:
        assert "|" not in d and "tee" not in d.split(), f"{name}: {d}"


# --- the two root locks, the validator's modes, the standalone recorder, the I10 exceptions ----


def test_the_two_root_locks_are_created_private_and_reasserted_before_the_flock():
    # Spec 08 / 06 I9's lock reasoning: flock takes LOCK_EX on a read-only descriptor, so a 0644
    # lock lets any local user stall every build or every update lap. Each script creates its lock
    # 0600 when absent, re-asserts 0600 every run (migrating an older lock), and only then opens
    # descriptor 9 on it.
    for script, lock, create_at_mode in (
        (_VALIDATOR, '"$LOCK"', 'install -m 0600 /dev/null "$LOCK"'),
        # autoupdate's lap tests stub `install` as a stage recorder, so it creates under umask 077.
        (_AUTOUPDATE, '"$ROOT/.autoupdate.lock"', '(umask 077; : > "$ROOT/.autoupdate.lock")'),
    ):
        stmts = _statements(script)
        create = _one(script, f"[ -e {lock} ] || {create_at_mode}")
        chmod = _one(script, f"chmod 0600 {lock}")
        opened = _one(script, f"exec 9>{lock}")
        assert stmts.index(create) < stmts.index(chmod) < stmts.index(opened), script.name


def test_the_validator_reasserts_its_modes_before_the_already_built_early_exit():
    # Spec 08: the unit's UMask covers only what a run creates; a 0700 prefix from an earlier run
    # under a strict umask keeps its build.log unreachable forever unless the early-exit path, the
    # only one that runs for a finished pin, re-asserts the modes first.
    stmts = _statements(_VALIDATOR)
    root = _one(_VALIDATOR, 'chmod 0755 "$ROOT"')
    every_pin = _one(_VALIDATOR, 'for prefix in "$ROOT"/*/; do')
    prefix = _one(_VALIDATOR, 'chmod 0755 "$prefix"')
    log = _one(_VALIDATOR, 'if [ -f "$prefix/build.log" ]; then chmod 0644 "$prefix/build.log"; fi')
    early_exit = _one(_VALIDATOR, 'log "pin $KEY already built at $slot"')
    assert stmts.index(root) < stmts.index(every_pin) < stmts.index(prefix) < stmts.index(log) < stmts.index(early_exit)


def test_the_crash_recorder_installer_names_the_parent_and_migrates_existing_records():
    # Spec 08 / 06 I9 on a standalone control host, where the apply's sweep never runs: the parent
    # is named in install -d (the mode applies to the leaf only), and existing records are brought
    # to 0644 — every regular file but the local-reset lock.
    stmts = [s.strip() for s in _statements(_CRASH_INSTALLER)]
    parent = "install -d -m 0755 /var/lib/tt-device-broker"
    leaf = "install -d -m 0755 /var/lib/tt-device-broker/health"
    assert stmts.count(parent) == 1 and stmts.count(leaf) == 1
    assert stmts.index(parent) < stmts.index(leaf)
    sweep = _one(_CRASH_INSTALLER, f"sweep_to_mode 0644 {SYSTEM_HEALTH_DIR} ")
    assert f"-maxdepth 1 -type f ! -name {recovery_base.LOCAL_RESET_LOCK}" in sweep
    # The apply's own race-safe helper (spec 08): this installer runs on broker hosts too, over a
    # directory the live broker writes; and best-effort, like the rest of it.
    source = '. "$(dirname "$(readlink -f "$0")")/tt-device-modes.sh"'
    assert stmts.index(source) < stmts.index(sweep.strip())
    assert "|| echo " in sweep and sweep.rstrip().endswith(">&2"), "a failure is reported, not fatal"


def test_the_buslock_unit_and_the_crash_recorder_write_to_the_health_dir_at_the_stated_paths():
    # Spec 06 I10 puts these two outside the one-file-plus-stderr shape and says where each writes:
    # the buslock unit appends perf's measurement to health/buslock.log; the crash recorder's
    # default health dir is the same directory (it runs on control hosts that have no log dir).
    exec_start = next(s for s in _statements(_BUSLOCK_UNIT) if s.startswith("ExecStart="))
    assert ">> /var/lib/tt-device-broker/health/buslock.log" in exec_start, exec_start
    recorder = _CRASH_RECORDER.read_text()
    assert (
        'HEALTH_DIR = Path(os.environ.get("TT_DEVICE_MCP_HEALTH_DIR", "/var/lib/tt-device-broker/health"))' in recorder
    )
    assert 'BUSLOCK_FILE = "buslock.log"' in recorder


# --- the deploy side names the broker's own constants -----------------------------------------


def _skipped_names(stmt: str) -> list[str]:
    return re.findall(r"! -name (\S+)", stmt)


def test_both_sweeps_skip_exactly_the_lock_the_broker_creates():
    # Spec 06 I9: the one private file is the broker's local-reset lock. A sweep that skipped a
    # different name would widen the real lock to 0644 the next time the constant was renamed.
    apply_files = _one(_APPLY, "sweep_to_mode 0644", "-type f")
    crash = _one(_CRASH_INSTALLER, "sweep_to_mode 0644", "-type f")
    for stmt in (apply_files, crash):
        assert _skipped_names(stmt) == [recovery_base.LOCAL_RESET_LOCK], stmt


def test_the_deploy_side_paths_are_the_brokers_system_path_constants(tmp_path):
    # Spec 06 I9 re-asserts the roots only when the broker's log and health dirs ARE the system
    # defaults. The unit, the apply's directories and sweep, the log helper's default, reconcile's
    # age-out and the crash recorder all spell those paths in shell; each must be the constant.
    exec_start = next(ln for ln in _render(tmp_path, _GALAXY).splitlines() if ln.startswith("ExecStart="))
    assert exec_start.split("--log-dir ", 1)[1].split()[0] == SYSTEM_LOG_DIR, exec_start
    loop = _one(_APPLY, "for tree in ")
    assert loop.split("for tree in ", 1)[1].split(";")[0].split() == [SYSTEM_LOG_DIR, SYSTEM_STATE_DIR]
    for path in (SYSTEM_LOG_DIR, SYSTEM_STATE_DIR, SYSTEM_HEALTH_DIR):
        assert any(s.strip() == f"install -d -m 0755 {path}" for s in _statements(_APPLY)), path
    assert any(
        s.strip() == f'dir="${{TTDEV_LOG_DIR:-{SYSTEM_LOG_DIR}}}"' for s in _statements(_HELPER)
    ), "log.sh's default log dir"
    assert any(s.strip() == f"LOGS={SYSTEM_LOG_DIR}" for s in _statements(_RECONCILE)), "reconcile's age-out dir"
    for path in (SYSTEM_STATE_DIR, SYSTEM_HEALTH_DIR):
        assert any(s.strip() == f"install -d -m 0755 {path}" for s in _statements(_CRASH_INSTALLER)), path


def _gnu_find() -> bool:
    try:
        return subprocess.run(["find", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


def test_reconcile_sweep_run_by_find_deletes_exactly_the_aged_job_logs(tmp_path):
    if not _gnu_find():
        pytest.skip("the age-out is GNU find's (-regextype); the statement anchors hold here")
    # Spec 08 (4b): the two statements as reconcile carries them, run by find over a stand-in log
    # dir. Their errors are swallowed in production, so a pattern find rejects or reads differently
    # from Python would silently stop the age-out; only running them proves it does not.
    month_ago = time.time() - 40 * 86400
    # The readers' near-miss list includes a rotated `.log.1.gz`, which the age-out alone deletes.
    rotated = [n for n in JOB_LOG_NAME_NEAR_MISSES if re.fullmatch(_JOB_LOG_GZ, n)]
    assert rotated, "the near-miss list must keep exercising the rotated form"
    old_jobs = ["2026-01-01_120000_007.log", "2026-01-01_120000_ubb-reset-3.log", *rotated]
    keep = ["2026-03-01_120000_008.log", *(n for n in JOB_LOG_NAME_NEAR_MISSES if n not in rotated)]
    for name in old_jobs + keep:
        f = tmp_path / name
        f.write_text("")
        if name != "2026-03-01_120000_008.log":
            os.utime(f, (month_ago, month_ago))
    sweeps = "\n".join(s.strip() for s in _sweep_statements())
    r = subprocess.run(
        ["bash", "-c", f'LOGS="$1"\n{sweeps}\n', "_", str(tmp_path)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(set(keep)), "only the aged job logs go"


@pytest.mark.parametrize(
    "script, kind",
    [
        (_AUTOUPDATE, "autoupdate_applied_no_restart"),
        (_AUTOUPDATE, "autoupdate_apply"),
        (_DEVICE_LOCK, "device_lock_drift"),
    ],
    ids=["autoupdate_applied_no_restart", "autoupdate_apply", "device_lock_drift"],
)
def test_a_shell_health_event_append_creates_the_journal_at_0644_under_a_strict_umask(tmp_path, script, kind):
    # Spec 06 I9: the broker rolls health_events.jsonl to .1, so the next shell append can be the
    # one that creates it. The append states the mode itself; run exactly as the script carries it,
    # under umask 077 with no journal yet, the new file is 0644.
    stmt = _one(script, f'"kind": "{kind}"', '>> "$EVENTS"')
    events = tmp_path / "health_events.jsonl"
    body = (
        'umask 077\nlog() { echo "$*" >&2; }\n'
        f"EVENTS={events}\nnow=1700000000.5; iso=x; installed_sha=a; remote_sha=b; paths_json=; nl=\n"
        "idle=true; drained_s=0; restart_rc=0\n"
        f"{stmt}\n"
    )
    r = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stderr == "", r.stderr
    assert f'"kind": "{kind}"' in events.read_text()
    assert stat.S_IMODE(events.stat().st_mode) == 0o644
