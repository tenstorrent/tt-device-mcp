# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Every Python and shell source carries an SPDX header near its top, so a missing one fails here
before CI's org-wide spdx-checker rejects the PR."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(
    p
    for d in ("src", "tests", "deploy")
    for pattern in ("*.py", "*.sh")
    for p in (ROOT / d).rglob(pattern)
    if "__pycache__" not in p.parts
)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_source_has_spdx_header(path):
    head = path.read_text(encoding="utf-8").splitlines()[:5]
    assert any("SPDX-License-Identifier:" in line for line in head), f"{path.relative_to(ROOT)}: no SPDX header"
