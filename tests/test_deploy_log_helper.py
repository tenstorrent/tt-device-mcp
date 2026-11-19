# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The deploy-script logging helper (`deploy/tt-device-log.sh`, staged as `$TTDEV_ROOT/log.sh`)
and the deploy scripts that source it (spec 06 I10): the ones that run under units, and
apply-host-config.sh, which runs on every autoupdate lap and from the installer.

The helper is one of the deploy scripts a test may run (spec 09 lists them): it is a pure shell function, driven here under
bash with `TTDEV_LOG_DIR` pointed at a temp dir, so no host path is touched (spec 09). The unit
scripts (`_UNIT_SCRIPTS` below) are never executed whole; their sourcing and their silence outside
`log` are asserted on statements through `deploy_helpers`.
"""

import calendar
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.deploy_helpers import _statements

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_HELPER = _DEPLOY / "tt-device-log.sh"
_RECONCILE = _DEPLOY / "tt-device-reconcile.sh"
_AUTOUPDATE = _DEPLOY / "tt-device-autoupdate.sh"
_VALIDATOR = _DEPLOY / "install-fabric-validator.sh"
_APPLY = _DEPLOY / "apply-host-config.sh"
_DEVICE_LOCK = _DEPLOY / "tt-device-lock.sh"

_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC \| (?P<name>[^|]+) \| (?P<msg>.*)$")
_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def _run(script: str, log_dir: Path, *, errexit: bool = False) -> subprocess.CompletedProcess:
    """Source the helper into a fresh bash, then run `script` in that shell."""
    flags = ["-e"] if errexit else []
    body = f'. "{_HELPER}"\n{script}\n'
    return subprocess.run(
        ["bash", *flags, "-c", body],
        env={**_ENV, "TTDEV_LOG_DIR": str(log_dir), "TTDEV_LOG_NAME": "x"},
        capture_output=True,
        text=True,
    )


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def test_log_appends_the_three_field_line_to_the_named_file_and_echoes_it_to_stderr(tmp_path):
    # `<YYYY-MM-DD HH:MM:SS UTC> | <name> | <message>` to $TTDEV_LOG_DIR/$TTDEV_LOG_NAME.log, and the
    # same bytes on stderr — the journal copy and the file are one line, written twice.
    log_dir = tmp_path / "logs"
    r = _run("log hello", log_dir)
    assert r.returncode == 0, r.stderr
    lines = (log_dir / "x.log").read_text().splitlines()
    assert len(lines) == 1
    m = _LINE.match(lines[0])
    assert m, lines[0]
    assert m.group("name") == "x" and m.group("msg") == "hello"
    assert r.stderr.splitlines() == lines, "the stderr copy is the same line"
    assert r.stdout == "", "nothing on stdout — the helper's two destinations are the file and stderr"


def test_the_stamp_is_utc_whatever_the_callers_zone(tmp_path):
    # The line says UTC, so it must be UTC under any TZ. A host or CI in UTC would pass a local-time
    # stamp too; `<+09>-9` is a POSIX zone 9 h east that needs no tzdata, so a stamp in local time
    # lands 9 h off the window.
    log_dir = tmp_path / "logs"
    t0 = int(time.time())
    r = subprocess.run(
        ["bash", "-c", f'. "{_HELPER}"\nlog hello\n'],
        env={**_ENV, "TZ": "<+09>-9", "TTDEV_LOG_DIR": str(log_dir), "TTDEV_LOG_NAME": "x"},
        capture_output=True,
        text=True,
    )
    t1 = int(time.time())
    assert r.returncode == 0, r.stderr
    line = (log_dir / "x.log").read_text().splitlines()[0]
    stamp = calendar.timegm(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    assert t0 <= stamp <= t1 + 1, (t0, line, t1)


def test_log_creates_an_absent_dir_0755_and_an_absent_file_0644_whatever_the_umask(tmp_path):
    # The mode is the helper's own (its umask-022 subshell), not the sourcing unit's umask: under
    # 077 the directory and the file still come out readable (spec 06 I9).
    log_dir = tmp_path / "logs"
    assert not log_dir.exists()
    r = _run("umask 077; log hello; log again", log_dir)
    assert r.returncode == 0, r.stderr
    assert _mode(log_dir) == 0o755
    assert _mode(log_dir / "x.log") == 0o644
    assert len((log_dir / "x.log").read_text().splitlines()) == 2, "the second call appends"


def test_the_append_itself_states_the_mode_so_a_rotation_between_create_and_append_leaves_0644(tmp_path):
    # logrotate renames the file between two lines, so the next `>>` is the one that creates it.
    # Under the caller's 077 umask that append still makes a 0644 file (spec 06 I9), the rotated
    # file keeps only what was written before the rename, and the caller's umask is untouched.
    log_dir = tmp_path / "logs"
    r = _run('umask 077; log before; mv "$TTDEV_LOG_DIR/x.log" "$TTDEV_LOG_DIR/x.log.1"; log after; umask', log_dir)
    assert r.returncode == 0, r.stderr
    new, rotated = log_dir / "x.log", log_dir / "x.log.1"
    assert [ln.rsplit(" | ", 1)[1] for ln in new.read_text().splitlines()] == ["after"]
    assert [ln.rsplit(" | ", 1)[1] for ln in rotated.read_text().splitlines()] == ["before"]
    assert _mode(new) == 0o644, "the file the append recreated after the rename is 0644"
    assert r.stdout.strip() in ("0077", "077"), "the subshell's umask never reaches the caller"


def test_an_empty_log_dir_set_after_sourcing_means_the_default_not_stderr_only(tmp_path):
    # reconcile.sh and autoupdate.sh source /etc/default/tt-device-broker after log.sh, so an empty
    # TTDEV_LOG_DIR can arrive after the helper is loaded; it still means the default dir. Run on
    # a copy whose one default literal points at a temp dir, so no host path is touched.
    default = "/var/log/tt-device-broker"
    text = _HELPER.read_text()
    assert text.count(default) == 1, "one default literal, so the copy below redirects all of it"
    helper = tmp_path / "log.sh"
    helper.write_text(text.replace(default, str(tmp_path / "default")))
    r = subprocess.run(
        ["bash", "-c", f'TTDEV_LOG_DIR="{tmp_path / "early"}"; . "{helper}"; TTDEV_LOG_DIR=""; log hello'],
        env={**_ENV, "TTDEV_LOG_NAME": "x"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "WARNING" not in r.stderr, r.stderr
    assert (tmp_path / "default" / "x.log").read_text().endswith("| x | hello\n")
    assert not (tmp_path / "early").exists(), "the value at source time is not frozen in"


def test_the_stderr_only_switch_keeps_the_line_and_creates_no_file_anywhere(tmp_path):
    # apply-host-config's --print-unit seam writes no host path, and its log lines must not either:
    # TTDEV_LOG_STDERR_ONLY=1 keeps the stderr copy and creates nothing, even with a log dir set.
    log_dir = tmp_path / "logs"
    body = f'. "{_HELPER}"\nlog hello\n'
    r = subprocess.run(
        ["bash", "-e", "-c", body],
        env={**_ENV, "TTDEV_LOG_DIR": str(log_dir), "TTDEV_LOG_STDERR_ONLY": "1", "TTDEV_LOG_NAME": "x"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    lines = r.stderr.splitlines()
    assert len(lines) == 1 and _LINE.match(lines[0]) and lines[0].endswith("| x | hello"), r.stderr
    assert list(tmp_path.iterdir()) == [], "nothing created"


def test_log_leaves_the_callers_umask_alone(tmp_path):
    # A stated mode is how the helper gets its 0644, so it never has to — and never does — change the
    # umask the sourcing script runs under.
    log_dir = tmp_path / "logs"
    r = _run("umask 027; log hello; umask", log_dir)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() in ("0027", "027")


def test_log_returns_zero(tmp_path):
    log_dir = tmp_path / "logs"
    r = _run('log hello; echo "rc=$?"', log_dir)
    assert r.returncode == 0, r.stderr
    assert "rc=0" in r.stdout


def test_an_unwritable_log_dir_keeps_the_stderr_copy_warns_once_and_never_ends_a_set_e_shell(tmp_path):
    # install-fabric-validator.sh runs under `set -e`; a log line that cannot land must never be the
    # statement that ends a script (spec 06 I7's contract applied to the helper). A regular FILE as
    # TTDEV_LOG_DIR fails every create and append even for root. The stderr copy still goes out, one
    # warning names the unwritable path, a second failure adds no second warning, and the shell
    # continues to the sentinel.
    not_a_dir = tmp_path / "logs"
    not_a_dir.write_text("")
    r = _run("log first; echo sentinel-1; log second; echo sentinel-2", not_a_dir, errexit=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["sentinel-1", "sentinel-2"], "the set -e shell did not exit"
    err = r.stderr.splitlines()
    # The WHOLE of stderr: one warning, then the two copies — no bash "Not a directory" noise from
    # the failed redirection on either call (2>/dev/null is applied before >> in the helper).
    assert len(err) == 3, err
    warning, *copies = err
    assert "WARNING" in warning and str(not_a_dir / "x.log") in warning, "the warning names the unwritable path"
    assert _LINE.match(warning) and _LINE.match(warning).group("name") == "x"
    assert [_LINE.match(ln).group("msg") for ln in copies] == ["first", "second"]
    assert not any("WARNING" in ln for ln in copies), "a second failure adds no second warning"
    assert not_a_dir.read_text() == "", "nothing was written into the file that stood where the dir should be"


def test_log_defaults_its_dir_to_the_system_log_dir():
    # An unset or empty TTDEV_LOG_DIR defaults to /var/log/tt-device-broker (spec 06 env-var index);
    # asserted on the statement, never by writing there. `:-`, so empty means unset as for every other
    # env var; stderr-only is its own switch.
    stmts = _statements(_HELPER)
    assert any(s.strip() == 'dir="${TTDEV_LOG_DIR:-/var/log/tt-device-broker}"' for s in stmts), stmts


def test_log_states_its_modes_with_umask_022_and_never_calls_install():
    # A stated mode, not a redirect that inherits the caller's umask: the dir and the file are made
    # under `umask 022` in a subshell. Never `install`: autoupdate's lap tests stand it in as a stage
    # recorder, and a log line must not read as a staged file.
    code = [s for s in _statements(_HELPER) if not s.lstrip().startswith("#")]
    assert any("(umask 022;" in s and "mkdir -p" in s and '>> "$file"' in s for s in code), code
    assert not any(re.search(r"\binstall\b", s) for s in code), code


# --- the scripts that source it -----------------------------------------------------------------

_SOURCE_BY_ADJACENCY = '. "$(dirname "$(readlink -f "$0")")/log.sh"'
# apply-host-config.sh is never staged: it runs from a repo or clone's deploy/ directory, where the
# helper sits beside it under its repo name.
_SOURCE_FROM_DEPLOY = '. "$(dirname "$(readlink -f "$0")")/tt-device-log.sh"'
_SCRIPTS = [
    (_RECONCILE, "reconcile", _SOURCE_BY_ADJACENCY),
    (_AUTOUPDATE, "autoupdate", _SOURCE_BY_ADJACENCY),
    (_VALIDATOR, "fabric-validator", _SOURCE_BY_ADJACENCY),
    (_APPLY, "apply-host-config", _SOURCE_FROM_DEPLOY),
    (_DEVICE_LOCK, "device-lock", _SOURCE_BY_ADJACENCY),
]
_IDS = [name for _, name, _ in _SCRIPTS]
_UNIT_SCRIPTS = [(s, n) for s, n, src in _SCRIPTS if src == _SOURCE_BY_ADJACENCY]


def _is_source(stmt: str, source: str) -> bool:
    """The source statement, alone or carrying the unit scripts' stderr-only fallback `log`."""
    stmt = stmt.strip()
    return stmt == source or (stmt.startswith(f"{source} 2>/dev/null") and "|| log() {" in stmt)


@pytest.mark.parametrize("script, name, source", _SCRIPTS, ids=_IDS)
def test_each_unit_script_sources_the_helper_by_adjacency_and_sets_its_name(script, name, source):
    # The helper is found next to the script, so no config key is needed (the validator sources
    # only its pin and has no TTDEV_ROOT). The name is the script's identity, set by the script,
    # before its first `log`.
    stmts = _statements(script)
    src = [i for i, s in enumerate(stmts) if _is_source(s, source)]
    assert len(src) == 1, f"{script.name} must source the helper by adjacency exactly once, got {stmts}"
    named = [i for i, s in enumerate(stmts) if s.strip() == f"TTDEV_LOG_NAME={name}"]
    assert len(named) == 1, f"{script.name} must set TTDEV_LOG_NAME={name} exactly once"
    first_log = next((i for i, s in enumerate(stmts) if re.search(r"(^|[;{(|&]\s*)log\s", s.strip())), None)
    assert first_log is not None, f"{script.name} never calls log"
    assert src[0] < named[0] < first_log, "source, then name, then the first log call"


def _is_code(stmt: str) -> bool:
    return bool(stmt.strip()) and not stmt.lstrip().startswith("#")


def _redirects_into_a_file(stmt: str) -> bool:
    """A `>` or `>>` whose target is a path (a `$VAR`, a quoted or bare path), not a descriptor."""
    return bool(re.search(r"(?<![0-9&<>])>>?\s*\"?(\$[A-Za-z_]|/|[A-Za-z_.])", stmt))


# Functions whose stdout is their answer, captured by the caller: data, not status lines.
_STDOUT_ANSWERS = {
    "tt-device-autoupdate.sh": ["restart_needed"],
    "tt-device-lock.sh": ["human_members", "nodes", "drifted"],
}


def _answer_bodies(script) -> list[str]:
    stmts = _statements(script)
    body: list[str] = []
    for fn in _STDOUT_ANSWERS.get(script.name, []):
        start = stmts.index(f"{fn}() {{")
        end = stmts.index("}", start)
        body += stmts[start:end]
    return body


@pytest.mark.parametrize("script", [s for s, _, _ in _SCRIPTS], ids=_IDS)
def test_each_unit_script_emits_no_status_line_outside_log(script):
    # No `echo ... >&2`, no bare `echo`/`printf` to stdout or stderr: every status line goes through
    # `log`, so the file and the journal agree. An echo or printf redirected INTO a file (autoupdate's
    # installed.sha and pending record, reconcile's stray-pid memo) is data, not a status line, and so
    # is the validator's `--key` answer on stdout: apply-host-config captures it as the pin's prefix
    # name.
    source = next(src for path, _, src in _SCRIPTS if path == script)
    answers = _answer_bodies(script)
    own = [s for s in filter(_is_code, _statements(script)) if not _is_source(s, source) and s not in answers]
    for stmt in own:
        # What a `$(...)` prints is captured, and `printf -v` assigns a variable: neither writes out.
        bare = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "", stmt)
        if not re.search(r"\b(echo|printf)\b", bare) or re.search(r"\bprintf -v\b", bare):
            continue
        assert ">&2" not in stmt, f"{script.name}: status line to stderr outside log: {stmt!r}"
        assert "/dev/stderr" not in stmt, f"{script.name}: status line to stderr outside log: {stmt!r}"
        if stmt.strip() == 'if [ "${2:-}" = --key ]; then echo "$KEY"; exit 0; fi':
            continue
        assert _redirects_into_a_file(stmt), f"{script.name}: bare echo/printf outside log: {stmt!r}"
    assert not any(
        re.search(r"(^|[^\w])log\s*\(\)", s) for s in own
    ), f"{script.name} must use the shared helper's log, not define its own"


@pytest.mark.parametrize("script, name", _UNIT_SCRIPTS, ids=[n for _, n in _UNIT_SCRIPTS])
def test_a_unit_script_without_its_helper_logs_to_stderr_and_carries_on(tmp_path, script, name):
    # Spec 06 I10: the unit scripts source log.sh by adjacency, which a checkout run or an upgrade
    # that staged the script before the helper does not have. Their source statement, run from a
    # directory with no log.sh under the strictest flags any of them uses, must still leave a `log`
    # that writes the three-field line to stderr, returns 0 and ends nothing.
    stmt = next(s for s in _statements(script) if _is_source(s, _SOURCE_BY_ADJACENCY))
    probe = tmp_path / f"{name}.sh"
    probe.write_text(f'set -euo pipefail\n{stmt}\nTTDEV_LOG_NAME={name}\nlog "still here"\necho after\n')
    r = subprocess.run(["bash", str(probe)], env=_ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "after", "the script must carry on past the log call"
    lines = r.stderr.strip().splitlines()
    assert len(lines) == 1, f"only the log line on stderr, no source error: {r.stderr!r}"
    m = _LINE.match(lines[0])
    assert m and m["name"] == name and m["msg"] == "still here", lines[0]
