# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""server.py and fsm.py reach the health package through its one facade
(tt_device_mcp.health) only — never a submodule directly. See health/__init__.py's own module
docstring for what is deliberately re-exported there and why; there is no allowlist here
because none is needed: every name either file needs either comes off that facade or (for
server.py) is a member of the subsystem ServerFsm.boot() constructs."""

import ast
from pathlib import Path

import pytest

FACADE_ONLY_FILES = ("src/tt_device_mcp/server.py", "src/tt_device_mcp/fsm.py", "src/tt_device_mcp/telemetry.py")

SUBMODULE_PREFIX = "tt_device_mcp.health."


def _health_submodule_imports(path: str) -> list:
    """Direct reaches past the facade, counting BOTH import forms.

    `import tt_device_mcp.health.monitors.fabric as fabric` binds the submodule just as
    effectively as taking a name out of it, so checking only `ast.ImportFrom` leaves the
    invariant this module asserts unenforced against half the syntax that can breach it.
    """
    tree = ast.parse(Path(path).read_text())
    offenders = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith(SUBMODULE_PREFIX):
            offenders.append(n.module)
        elif isinstance(n, ast.Import):
            offenders += [a.name for a in n.names if a.name.startswith(SUBMODULE_PREFIX)]
    return offenders


@pytest.mark.parametrize("path", FACADE_ONLY_FILES)
def test_imports_only_the_facade(path):
    offenders = _health_submodule_imports(path)
    assert offenders == [], f"{path} reaches into health internals: {offenders}"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("from tt_device_mcp.health.monitors import fabric", "tt_device_mcp.health.monitors"),
        ("import tt_device_mcp.health.monitors.fabric", "tt_device_mcp.health.monitors.fabric"),
        ("import tt_device_mcp.health.monitors.fabric as fabric", "tt_device_mcp.health.monitors.fabric"),
    ],
)
def test_the_checker_catches_every_breaching_form(tmp_path, line, expected):
    """Negative control: without it, a checker blind to one import form still reports a clean
    facade and the test above passes for the wrong reason."""
    probe = tmp_path / "probe.py"
    probe.write_text(line + "\n")
    assert _health_submodule_imports(str(probe)) == [expected]


def test_the_facade_itself_is_not_an_offender():
    """`from tt_device_mcp.health import X` is the sanctioned route and must stay allowed —
    otherwise the check would forbid the very facade it exists to enforce."""
    probe = Path(__file__).parent / "_facade_probe.py"
    try:
        probe.write_text("from tt_device_mcp.health import HealthMonitor\n" "import tt_device_mcp.health\n")
        assert _health_submodule_imports(str(probe)) == []
    finally:
        probe.unlink(missing_ok=True)
