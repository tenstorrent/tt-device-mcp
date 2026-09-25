# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared shell-script assertions for the test_deploy_* fail-closed suites."""

from pathlib import Path


def _statements(script: Path) -> list[str]:
    """Logical shell statements: backslash-continued source lines joined into one string,
    so an `... || { ...; exit 1; }` spread over two lines reads as a single unit."""
    out: list[str] = []
    buf = ""
    for line in script.read_text().splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _one(script: Path, *needles: str) -> str:
    """The single logical statement containing every needle."""
    hits = [s for s in _statements(script) if all(n in s for n in needles)]
    assert len(hits) == 1, f"expected exactly one statement matching {needles} in {script.name}, got {hits}"
    return hits[0]
