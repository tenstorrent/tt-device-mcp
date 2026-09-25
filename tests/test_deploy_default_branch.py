# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The installer + auto-update default must track `main`, not a dev branch.

A fresh install (or a host that never overrode TTDEV_BRANCH) autoupdates from
whatever branch these defaults name. Once a dev branch is promoted and deleted,
a default that still points at it clones a branch that no longer exists. Pin the
default to `main` everywhere it is shipped in-repo.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BROKER = _ROOT / "deploy" / "install-tt-device-broker.sh"
_USER = _ROOT / "deploy" / "install-user.sh"
SRC_PATH = str(_ROOT / "src")

# Every in-repo TTDEV_BRANCH fallback, whatever branch it names.
_BRANCH_DEFAULT = re.compile(r"TTDEV_BRANCH:-([^}\"'\s]+)")


def test_broker_installer_defaults_branch_to_main():
    text = _BROKER.read_text()
    assert 'TTDEV_BRANCH="${TTDEV_BRANCH:-main}"' in text


def test_user_installer_defaults_branch_to_main():
    text = _USER.read_text()
    assert "${TTDEV_BRANCH:-main}" in text


def test_no_in_repo_default_tracks_anything_but_main():
    """Stronger than naming one retired branch: any default other than `main` fails, so a dev
    branch that gets deleted on promotion cannot survive here under any name."""
    for rel in (
        "deploy/install-tt-device-broker.sh",
        "deploy/install-user.sh",
        "src/tt_device_mcp/cli.py",
        "README.md",
    ):
        for branch in _BRANCH_DEFAULT.findall((_ROOT / rel).read_text()):
            assert branch == "main", f"{rel} defaults TTDEV_BRANCH to {branch!r}, not 'main'"
