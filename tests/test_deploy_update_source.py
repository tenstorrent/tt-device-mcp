# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The update source is a constant, not configuration; only the branch is settable (08 I16).

Autoupdate installs code as root on a timer, so "where does that code come from" has to have
one answer that an operator cannot change. A source that can be set can be set to a private
repository, and a private repository needs a credential — a key for root to hold, or somebody
else's to borrow. A literal URL removes the question.

These tests assert on the shipped scripts' own logical statements, the house pattern for
shell: git's behaviour is git's contract, and a test that re-derived it would pass with
`deploy/` deleted. What is ours is that the source is a literal, that the clone is somewhere
only root can write, and that a lap installs the commit it announced.

The one exception runs shell, because it runs OUR shell: the round-trip executes the
installer's own heredoc against a hostile branch name. `/etc/default/tt-device-broker` is
`.`-sourced by root once a minute, so a value that closes its own literal does not merely
truncate — the remainder is parsed as shell.
"""

import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from tests.deploy_helpers import _statements

_ROOT = Path(__file__).resolve().parent.parent
_AUTOUPDATE = _ROOT / "deploy" / "tt-device-autoupdate.sh"
_INSTALLER = _ROOT / "deploy" / "install-tt-device-broker.sh"

PUBLIC_URL = "https://github.com/tenstorrent/tt-device-mcp.git"


def _code(script: Path) -> list[str]:
    """Statements with comment-only lines dropped."""
    return [s for s in _statements(script) if not s.lstrip().startswith("#")]


def _one_code(script: Path, *needles: str) -> str:
    """The single executable statement containing every needle."""
    hits = [s for s in _code(script) if all(n in s for n in needles)]
    assert len(hits) == 1, f"expected exactly one statement matching {needles}, got {hits}"
    return hits[0]


# --- the source is a constant ----------------------------------------------------------------


def test_the_update_source_is_a_literal_with_no_expansion():
    """The invariant, stated the strong way. Asserting that one particular env var is unread
    would only forbid that name; asserting the assignment expands nothing forbids every name,
    including whichever one a future convenience would reach for."""
    stmt = _one_code(_AUTOUPDATE, "readonly REPO=")
    assert PUBLIC_URL in stmt, f"the update source is not the public repo: {stmt}"
    assert "$" not in stmt, f"the update source interpolates something, so it is configuration, not a constant: {stmt}"


def test_nothing_else_assigns_the_source():
    """A second assignment further down would quietly win over the readonly literal — or fail
    the script at runtime, which the timer would swallow as a silent no-op lap."""
    assigns = [s for s in _code(_AUTOUPDATE) if re.match(r"\s*(readonly\s+)?REPO=", s)]
    assert len(assigns) == 1, f"the source is assigned more than once: {assigns}"


def test_the_only_operator_input_in_the_config_is_the_branch():
    """Whatever the installer writes into /etc/default is what a later edit can reach. Pinning
    the whole key set — rather than the absence of any one key — is what keeps a new knob from
    arriving without someone deciding it should be settable."""
    stmts = _statements(_INSTALLER)
    start = next(i for i, s in enumerate(stmts) if "cat > /etc/default/tt-device-broker" in s)
    end = next(i for i, s in enumerate(stmts) if i > start and s.strip() == "CONF")
    keys = {s.split("=", 1)[0] for s in stmts[start + 1 : end] if "=" in s}
    assert keys == {
        "TTDEV_ROOT",
        "TTDEV_VENV",
        "TTDEV_RESET_MODE",
        "TTDEV_FABRIC_CHECK_CMD",
        "TTDEV_FABRIC_DESCRIPTOR",
        "TTDEV_AUTOUPDATE",
        "TTDEV_BRANCH",
    }, f"the written config gained or lost a key: {sorted(keys)}"


def test_the_clone_lives_under_the_broker_root_never_in_a_home():
    """What lands in the clone is pip-installed as root on the same lap, so it may not sit
    anywhere an unprivileged user can write. The retired shape put it in `~/.cache`."""
    assert any('CLONE="$ROOT/src"' in s for s in _code(_AUTOUPDATE)), "the clone moved out of the broker root"
    assert not any("$HOME" in s or "/.cache/" in s for s in _code(_AUTOUPDATE))


def test_the_clone_is_created_private():
    """`umask 077` in the subshell, so the tree is 0700 from creation rather than chmod'd
    afterwards — a window where it is world-readable is a window where it is world-readable."""
    stmt = _one_code(_AUTOUPDATE, "umask 077", "git clone")
    assert "$CLONE" in stmt, stmt


# --- the lap installs what it announced ----------------------------------------------------


def test_the_lap_installs_the_sha_it_resolved():
    """The idle gate can defer for minutes. Resetting to `origin/$BR` afterwards would install
    a commit this lap never resolved, never printed, and that `installed.sha` then misnames."""
    stmt = _one_code(_AUTOUPDATE, "reset --hard")
    assert '"$remote_sha"' in stmt, f"the reset does not pin the resolved sha: {stmt}"
    assert "origin/" not in stmt, f"the reset re-reads the branch after the gate: {stmt}"


def test_every_unreachable_source_is_a_no_op_lap_that_says_why():
    """A host that cannot clone, cannot reach the network, or tracks a branch nobody pushed is
    misconfigured or offline — report and retry next minute, never install anything, and never
    exit non-zero into the timer's face."""
    for needles in (("cannot clone",), ("cannot reach",), ("has no branch",)):
        stmt = _one_code(_AUTOUPDATE, *needles)
        assert "exit 0" in stmt, f"a no-op lap exits non-zero: {stmt}"


def test_a_leftover_non_repository_is_cleared_rather_than_retried_forever():
    """git will not clone into a non-empty directory, so an interrupted first clone would
    otherwise fail every lap until someone logged in and deleted it by hand."""
    stmts = _code(_AUTOUPDATE)
    guard = next(i for i, s in enumerate(stmts) if '[ ! -d "$CLONE/.git" ]' in s)
    clone_at = next(i for i, s in enumerate(stmts) if i > guard and "git clone" in s)
    assert any(
        "rm -rf" in s and "$CLONE" in s for s in stmts[guard:clone_at]
    ), "nothing clears a leftover that is not a repository before cloning into it"


# --- the branch is still operator input, and still escaped -----------------------------------


def test_the_branch_is_written_bash_escaped():
    """Quote characters are not escaping. `TTDEV_BRANCH` is now the only operator-supplied
    value in the config, and a branch name may legally contain an apostrophe."""
    text = _INSTALLER.read_text()
    assert 'TTDEV_BRANCH=$(printf %q "$TTDEV_BRANCH")' in text
    assert "TTDEV_BRANCH='$TTDEV_BRANCH'" not in text, "wrapping in quote characters is not escaping"


def test_the_config_round_trips_a_branch_containing_an_apostrophe():
    """The installer's own heredoc, run against a hostile value and sourced the way every
    consumer sources it."""
    stmts = _statements(_INSTALLER)
    start = next(i for i, s in enumerate(stmts) if "cat > /etc/default/tt-device-broker" in s)
    end = next(i for i, s in enumerate(stmts) if i > start and s.strip() == "CONF")
    heredoc = "\n".join(stmts[start : end + 1]).replace("cat > /etc/default/tt-device-broker", 'cat > "$OUT"')
    hostile = "feature/it's-fine'; touch \"$CANARY\"; : '"

    with tempfile.TemporaryDirectory() as tmp:
        out, canary = Path(tmp) / "conf", Path(tmp) / "canary"
        # No `set -u`: the heredoc reads installer-internal values this test does not stand up,
        # and naming them here would couple the test to lines it is not about.
        script = "\n".join(
            [
                f"TTDEV_BRANCH={shlex.quote(hostile)}",
                f"OUT={shlex.quote(str(out))}",
                f"CANARY={shlex.quote(str(canary))}",
                heredoc,
                f". {shlex.quote(str(out))}",
                'printf %s "$TTDEV_BRANCH"',
            ]
        )
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        assert not canary.exists(), "sourcing the written config executed part of the value"

    assert done.stdout == hostile, "the branch did not survive the round trip"
