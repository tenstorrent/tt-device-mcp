# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Every test a spec names as its anchor must exist (CONTRIBUTING; 09 I4).

The `## Test anchors` tables are the only machine-readable link between an invariant and the
test that holds it — 121 invariants, ~700 references, and fewer than 2% of tests name their
invariant in their own text. So the tables carry the whole relationship, and they point the
fragile way round: renaming a test breaks an anchor in a file the rename never touches, and
nothing notices. Six had rotted before this test existed, three of them naming tests that
never reached this branch at all, and a code reviewer found them rather than CI.

This does not check that an anchor is *apt* — only that it resolves. Aptness needs a reader.
What it removes is the failure where a table keeps asserting coverage that is not there.

Deliberately not asserting the converse (every invariant has an anchor): plenty stand on the
code by design and say so, and a few are honestly marked unanchored. Turning that into a
failure would force a fake anchor rather than an honest gap.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ANCHOR = re.compile(r"`tests/([\w/]+\.py)::([\w\[\]\-.]+)`")
_TESTDEF = re.compile(r"^\s*(?:async )?def (test_\w+)", re.M)


def _defined_tests() -> set[str]:
    names: set[str] = set()
    for f in (_ROOT / "tests").rglob("test_*.py"):
        names |= set(_TESTDEF.findall(f.read_text()))
    return names


def test_every_spec_anchor_names_a_test_that_exists():
    defined = _defined_tests()
    dangling = []
    for spec in sorted((_ROOT / "specs").glob("*.md")):
        for lineno, line in enumerate(spec.read_text().split("\n"), 1):
            for rel, node in _ANCHOR.findall(line):
                # A parametrized anchor names the id in brackets; the function is what must exist.
                fn = node.split("[", 1)[0]
                if fn not in defined:
                    dangling.append(f"{spec.name}:{lineno} -> tests/{rel}::{node}")
    assert not dangling, "spec anchors naming tests that do not exist:\n  " + "\n  ".join(dangling)


def test_every_anchor_points_at_a_test_file_that_exists():
    """A renamed or deleted test *file* is the other half: the node id can still look plausible
    while the path is gone, and then the anchor is unresolvable for a different reason."""
    missing = []
    for spec in sorted((_ROOT / "specs").glob("*.md")):
        for lineno, line in enumerate(spec.read_text().split("\n"), 1):
            for rel, _ in _ANCHOR.findall(line):
                if not (_ROOT / "tests" / Path(rel).name).exists() and not (_ROOT / rel).exists():
                    missing.append(f"{spec.name}:{lineno} -> tests/{rel}")
    assert not missing, "spec anchors naming test files that do not exist:\n  " + "\n  ".join(missing)
