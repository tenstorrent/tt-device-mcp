# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The update source: the public repo, or a root-owned local bare mirror under the broker root (08 I16).

Each lap below runs the script's own source block, from the source choice to the sha it resolves,
against real git repositories in tmp_path: a bare "public" repo and, where a test makes one, a bare
mirror at $ROOT/mirror.git holding a different commit, so the resolved sha says which one a lap
fetched. The ownership check's uid is the one seam replaced (au_root_uid), so the tests run
unprivileged; the symlink, mode and bareness checks run as shipped.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.deploy_helpers import _clock
from tests.test_deploy_update_source import _code, _one_code

# Fork-heavy deploy-script laps: one xdist worker runs them all (conftest: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-autoupdate.sh"
_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd, *args):
    env = {**_GIT_ENV, "HOME": str(cwd)}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True).stdout.strip()


def _block():
    text = _SCRIPT.read_text()
    return text[text.index("# The source this lap (I16)") : text.index("# Read as $(cat) would")]


@pytest.fixture
def box(tmp_path):
    """A public bare repo at commit `public`, a work tree to fill a mirror from with commit `mirror`, and an
    empty broker root."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "f").write_text("1\n")
    _git(work, "add", "f")
    _git(work, "commit", "-q", "-m", "public")
    public = tmp_path / "public.git"
    _git(tmp_path, "init", "-q", "--bare", str(public))
    _git(work, "push", "-q", str(public), "main")
    pub_sha = _git(work, "rev-parse", "HEAD")
    (work / "f").write_text("2\n")
    _git(work, "commit", "-q", "-am", "mirror")
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o755)
    (tmp_path / "logs").mkdir()
    return {
        "tmp": tmp_path,
        "work": work,
        "public": public,
        "root": root,
        "pub": pub_sha,
        "mirror": _git(work, "rev-parse", "HEAD"),
    }


def _make_mirror(box, *, bare=True, branch="main"):
    mirror = box["root"] / "mirror.git"
    if bare:
        _git(box["tmp"], "init", "-q", "--bare", str(mirror))
        if branch:
            _git(box["work"], "push", "-q", str(mirror), f"main:{branch}")
    else:
        _git(box["tmp"], "init", "-q", "-b", "main", str(mirror))
    mirror.chmod(0o755)
    return mirror


def _lap(box, *, now=10_000, root_uid=None, repo=None, branch="main"):
    """One lap of the source block. Returns (exit code, resolved sha, stderr lines, systemctl calls)."""
    root, tmp = box["root"], box["tmp"]
    calls = tmp / "systemctl.calls"
    uid = os.getuid() if root_uid is None else root_uid
    prelude = f"""set -u
ROOT={root}; CLONE={root}/src; BR={branch}; REPO=file://{repo or box["public"]}
{_clock(_SCRIPT.read_text())}au_now() {{ printf -v "$1" %s {now}; }}
au_root_uid() {{ printf -v "$1" %s {uid}; }}
logger() {{ :; }}
systemctl() {{ echo "$*" >> {calls}; }}
"""
    done = subprocess.run(
        ["bash", "-c", prelude + _block() + 'echo "RESOLVED=$remote_sha"\n'],
        capture_output=True,
        text=True,
        timeout=60,
        env={**_GIT_ENV, "HOME": str(tmp), "TTDEV_LOG_DIR": str(tmp / "logs")},
    )
    m = re.search(r"^RESOLVED=(\S*)$", done.stdout, re.M)
    lines = [x for x in done.stderr.splitlines() if x.startswith("autoupdate:")]
    sysctl = calls.read_text().splitlines() if calls.exists() else []
    return done.returncode, m.group(1) if m else None, lines, sysctl


def _origin(box):
    return _git(box["root"] / "src", "config", "--get", "remote.origin.url")


def _decisions(box):
    log = box["tmp"] / "logs" / "autoupdate.log"
    return [x for x in log.read_text().splitlines() if "decision=source" in x] if log.exists() else []


# --- which source ----------------------------------------------------------------------------


def test_no_mirror_fetches_the_public_repo(box):
    rc, sha, _, _ = _lap(box)
    assert (rc, sha) == (0, box["pub"])
    assert _origin(box) == f"file://{box['public']}"


def test_a_valid_mirror_is_the_source_from_the_first_clone_on(box):
    _make_mirror(box)
    rc, sha, _, _ = _lap(box)
    assert (rc, sha) == (0, box["mirror"])
    assert _origin(box) == f"file://{box['root']}/mirror.git"


@pytest.mark.parametrize(
    "spoil",
    ["symlink", "mirror_group_writable", "mirror_world_writable", "root_group_writable", "not_root_owned", "not_bare"],
)
def test_a_mirror_root_alone_cannot_have_filled_is_ignored(box, spoil):
    """Each way an unprivileged user could have chosen what root installs: the lap fetches the public repo."""
    uid = None
    if spoil == "symlink":
        elsewhere = box["tmp"] / "elsewhere.git"
        _git(box["tmp"], "init", "-q", "--bare", str(elsewhere))
        _git(box["work"], "push", "-q", str(elsewhere), "main")
        (box["root"] / "mirror.git").symlink_to(elsewhere)
    elif spoil == "not_bare":
        _make_mirror(box, bare=False)
    else:
        mirror = _make_mirror(box)
        if spoil == "mirror_group_writable":
            mirror.chmod(0o775)
        elif spoil == "mirror_world_writable":
            mirror.chmod(0o757)
        elif spoil == "root_group_writable":
            box["root"].chmod(0o775)
        else:
            uid = os.getuid() + 1
    rc, sha, _, _ = _lap(box, root_uid=uid)
    assert (rc, sha) == (0, box["pub"]), spoil


def test_the_clone_origin_follows_the_choice_both_ways_and_the_decision_log_says_so(box):
    _lap(box)
    assert _origin(box) == f"file://{box['public']}"
    _make_mirror(box)
    rc, sha, lines, _ = _lap(box)
    assert (rc, sha) == (0, box["mirror"])
    assert _origin(box) == f"file://{box['root']}/mirror.git"
    assert any("source" in x and "mirror.git" in x for x in lines), lines
    (box["root"] / "mirror.git").chmod(0o777)  # spoiled: back to the public repo
    rc, sha, _, _ = _lap(box)
    assert (rc, sha) == (0, box["pub"])
    assert _origin(box) == f"file://{box['public']}"
    log = _decisions(box)
    assert len(log) == 2, log
    assert log[0].endswith(f"-> file://{box['root']}/mirror.git") and log[1].endswith(f"-> file://{box['public']}")
    _lap(box)
    assert len(_decisions(box)) == 2, "a lap with no switch logged one"


def test_a_mirror_without_the_branch_does_not_fall_back_to_the_public_repo(box):
    _make_mirror(box, branch="other")
    rc, sha, lines, _ = _lap(box)
    assert (rc, sha) == (0, None)
    assert lines == [f"autoupdate: cannot clone file://{box['root']}/mirror.git (main)"]
    _lap(box, root_uid=os.getuid() + 1)  # a clone from the public repo, then a valid mirror again
    rc, sha, lines, _ = _lap(box)
    assert (rc, sha) == (0, None)
    assert lines[-1] == f"autoupdate: cannot reach file://{box['root']}/mirror.git", lines


# --- an unreachable source is said once per outage --------------------------------------------


def test_an_unreachable_source_is_logged_once_over_five_laps_then_reachable_again_once(box):
    _lap(box)  # a clone exists, as on a running box
    gone = box["tmp"] / "gone.git"
    said = []
    for i in range(5):
        rc, sha, lines, calls = _lap(box, now=10_000 + 60 * i, repo=gone)
        assert (rc, sha, calls) == (0, None, []), "a failed fetch made a side effect"
        said += lines
    assert said == [
        f"autoupdate: source file://{box['public']} -> file://{gone}",
        f"autoupdate: cannot reach file://{gone}",
    ]
    box["public"].rename(gone)
    rc, sha, lines, _ = _lap(box, now=10_000 + 60 * 5, repo=gone)
    assert (rc, sha) == (0, box["pub"])
    assert lines == [f"autoupdate: file://{gone} reachable again after 300s"]
    rc, sha, lines, _ = _lap(box, now=10_000 + 60 * 6, repo=gone)
    assert (rc, sha, lines) == (0, box["pub"], [])
    assert not (box["root"] / ".source-unreachable").exists()


def test_a_missing_branch_is_logged_once_per_outage(box):
    """git fails the fetch of a branch the source lacks, so it reads as the source failing."""
    _lap(box)
    said = []
    for i in range(3):
        rc, sha, lines, calls = _lap(box, now=10_000 + 60 * i, branch="nobranch")
        assert (rc, sha, calls) == (0, None, [])
        said += lines
    assert said == [f"autoupdate: cannot reach file://{box['public']}"]


def test_a_failed_fetch_takes_no_hold_and_restarts_nothing():
    """Statically too: the source block exits before the dispatch hold and the restart, and calls neither."""
    block = _block()
    assert "systemctl" not in block and "HOLD" not in block
    text = _SCRIPT.read_text()
    assert text.index("# The source this lap (I16)") < text.index("# Dispatch hold (spec 08 I18")


# --- both sources are literals ----------------------------------------------------------------


def test_the_mirror_is_a_fixed_path_under_the_broker_root():
    stmt = _one_code(_SCRIPT, "MIRROR=")
    assert stmt.strip() == 'MIRROR="$ROOT/mirror.git"', stmt
    assigns = [s for s in _code(_SCRIPT) if re.match(r"\s*(readonly\s+)?(src|MIRROR)=", s)]
    assert [s.strip() for s in assigns] == ['MIRROR="$ROOT/mirror.git"', 'src="$REPO"'], assigns
    assert 'then src="file://$MIRROR"; fi' in _one_code(_SCRIPT, "if mirror_ok; then")


def test_every_git_call_that_touches_a_source_refuses_credentials():
    for needles in (("git clone",), ("fetch -q origin",), ("remote set-url",), ("--is-bare-repository",)):
        stmt = _one_code(_SCRIPT, *needles)
        assert "GIT_TERMINAL_PROMPT=0" in stmt and "GIT_ASKPASS=true" in stmt, stmt
