# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Every Python file carries the SPDX header the CI spdx-checker requires.

The checker runs only in CI, so a file added without the header passed every local check and
failed the PR (spec 09, CI). This catches it in the device-free suite.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE_LINE = "# SPDX-License-Identifier: Apache-2.0"


def test_every_python_file_has_the_spdx_header():
    missing = [
        str(p.relative_to(ROOT))
        for d in ("src", "tests")
        for p in sorted((ROOT / d).rglob("*.py"))
        if "__pycache__" not in p.parts and LICENSE_LINE not in p.read_text().splitlines()[:5]
    ]
    assert missing == [], f"files without the SPDX header: {missing}"
