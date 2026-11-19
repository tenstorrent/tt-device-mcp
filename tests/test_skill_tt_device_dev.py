# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The tt-device-dev skill: its front matter loads, and its setup script is idempotent.

The script runs against stand-ins for every outside command (tt-device-mcp, claude, gh) on a PATH
that holds only them and the system dirs, so nothing here reaches a broker, Claude or GitHub.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_SKILL = Path(__file__).resolve().parent.parent / "skills" / "tt-device-dev"
_SCRIPT = _SKILL / "scripts" / "setup-dev-env.sh"

_CLAUDE_STUB = """#!/bin/sh
echo "claude $*" >> "$STUB_LOG"
case "$2" in
  get) [ -f "$STUB_STATE/mcp" ] && exit 0; exit 1 ;;
  add) touch "$STUB_STATE/mcp" ;;
esac
"""
_GH_STUB = """#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "auth status") exit 0 ;;
  "repo view") echo acme/proj ;;
  "label list") cat "$STUB_STATE/labels" 2>/dev/null ;;
  "label create") echo "$3" >> "$STUB_STATE/labels" ;;
esac
"""


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text(body)
    p.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    """A workspace holding a tt-metal checkout and a git repo, plus stand-ins on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _stub(bin_dir, "tt-device-mcp", "#!/bin/sh\nexit 0\n")
    _stub(bin_dir, "claude", _CLAUDE_STUB)
    _stub(bin_dir, "gh", _GH_STUB)

    ws = tmp_path / "ws"
    py = ws / "tt-metal" / "python_env" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    py.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(ws)], check=True)

    e = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "STUB_STATE": str(state),
        "TTDEV_ROOT": str(tmp_path / "no-broker"),
        "TT_DEVICE_MCP_INSTALL_DIR": str(tmp_path / "no-user-install"),
    }
    return {"ws": ws, "env": e, "bin": bin_dir, "log": tmp_path / "stub.log"}


def _run(env, *args):
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=env["ws"],
        env=env["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshot(ws: Path) -> dict:
    return {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in ws.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(ws).parts
    }


def test_skill_front_matter_loads():
    text = (_SKILL / "SKILL.md").read_text()
    assert text.startswith("---\n")
    meta = yaml.safe_load(text.split("---\n", 2)[1])
    assert meta["name"] == _SKILL.name
    assert 0 < len(meta["description"]) <= 1024


def test_check_changes_nothing_and_reports_pending(env):
    before = _snapshot(env["ws"])
    r = _run(env, "--check")
    assert r.returncode == 1, r.stdout
    assert "WOULD FIX" in r.stdout and "MISSING" not in r.stdout
    assert _snapshot(env["ws"]) == before
    log = env["log"].read_text() if env["log"].exists() else ""
    assert "mcp add" not in log and "label create" not in log


def test_fix_then_rerun_is_idempotent(env):
    claude_md = env["ws"] / "CLAUDE.md"
    claude_md.write_text("# Mine\n\nkeep me\n")

    first = _run(env)
    assert first.returncode == 0, first.stdout
    assert "FIXED" in first.stdout
    for name in ("project.md", "tt-device.env.yaml", ".gitignore"):
        assert (env["ws"] / name).is_file(), name
    project = (env["ws"] / "project.md").read_text()
    assert "SPDX" not in project and project.startswith("# Project:")
    body = claude_md.read_text()
    assert body.startswith("# Mine\n\nkeep me\n")
    assert "<!-- tt-device-dev:begin -->" in body and "SPDX" not in body
    env_yaml = yaml.safe_load((env["ws"] / "tt-device.env.yaml").read_text())
    assert env_yaml["TT_METAL_HOME"] == f"{env['ws']}/tt-metal"
    assert {"PYTHONPATH", "PYTHON_ENV_DIR", "TT_METAL_CACHE", "TT_METAL_ENV"} <= set(env_yaml)

    after_first = _snapshot(env["ws"])
    second = _run(env)
    assert second.returncode == 0, second.stdout
    assert "FIXED" not in second.stdout and "WOULD FIX" not in second.stdout
    assert _snapshot(env["ws"]) == after_first
    assert _run(env, "--check").returncode == 0


def test_stale_claude_md_block_is_refreshed_in_place(env):
    claude_md = env["ws"] / "CLAUDE.md"
    claude_md.write_text("top\n<!-- tt-device-dev:begin -->\nold rules\n<!-- tt-device-dev:end -->\nbottom\n")
    r = _run(env)
    assert r.returncode == 0, r.stdout
    body = claude_md.read_text()
    assert body.startswith("top\n<!-- tt-device-dev:begin -->\n## Development workflow")
    assert body.endswith("<!-- tt-device-dev:end -->\nbottom\n")
    assert "old rules" not in body and body.count("tt-device-dev:begin") == 1


def test_incomplete_env_file_is_reported_not_overwritten(env):
    env_file = env["ws"] / "tt-device.env.yaml"
    env_file.write_text("TT_METAL_HOME: /x\nHF_HOME: /weights\n")
    r = _run(env)
    assert r.returncode == 1
    assert "MISSING" in r.stdout and "PYTHON_ENV_DIR" in r.stdout
    assert env_file.read_text() == "TT_METAL_HOME: /x\nHF_HOME: /weights\n"


def test_without_gh_labels_are_missing_and_nothing_is_created(env):
    os.remove(env["bin"] / "gh")
    r = _run(env)
    assert r.returncode == 1
    lines = {line.split()[0] + " " + line[11:39].strip() for line in r.stdout.splitlines() if line[:1].isupper()}
    assert "MISSING gh CLI" in lines
    assert "MISSING issue labels" in lines
