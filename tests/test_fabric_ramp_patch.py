# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The fabric validator's traffic-start ramp (deploy/fabric-validator-ramp.patch, spec 08 I30).

The pinned tt-metal source is not in the test environment, so the patch is checked against the
pre-image its own hunks require (their context and removed lines), with a real `git apply`. Set
TTDEV_RAMP_PATCH_TT_METAL to a tt-metal checkout at the pinned SHA to also check it against that."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tt_device_mcp.health.monitors import fabric

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_PATCH = _DEPLOY / "fabric-validator-ramp.patch"
_FILE = "tools/scaleout/validation/utils/cluster_validation_utils.cpp"
_GIT = shutil.which("git", path=os.defpath)


def _hunks(text: str) -> list[list[str]]:
    hunks: list[list[str]] = []
    for line in text[text.index("--- a/") :].splitlines():
        if line.startswith("@@"):
            hunks.append([])
        elif hunks and line[:1] in (" ", "-", "+"):
            hunks[-1].append(line)
    return hunks


def _pre_image(text: str) -> str:
    """A file holding exactly what each hunk needs, at the line numbers its header names."""
    lines: list[str] = []
    for header, body in zip(re.findall(r"^@@ -(\d+)", text, re.M), _hunks(text)):
        start = int(header) - 1
        lines += ["// filler"] * (start - len(lines))
        lines += [ln[1:] for ln in body if ln[0] in " -"]
    return "\n".join(lines + ["// end"]) + "\n"


def _apply_check(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([_GIT, "-C", str(tree), "apply", "--check", str(_PATCH)], capture_output=True, text=True)


def test_ramp_patch_is_the_one_the_pin_names():
    pin = (_DEPLOY / "fabric-validator.pin").read_text()
    assert re.search(r'^TTDEV_VALIDATOR_PATCHES="([^"]*)"', pin, re.M).group(1).split() == [_PATCH.name]


def test_ramp_patch_applies_to_the_source_lines_it_needs(tmp_path):
    text = _PATCH.read_text()
    assert re.findall(r"^--- a/(\S+)$", text, re.M) == [_FILE], "one file, the workload launcher"
    (tmp_path / _FILE).parent.mkdir(parents=True)
    (tmp_path / _FILE).write_text(_pre_image(text))
    res = _apply_check(tmp_path)
    assert res.returncode == 0, res.stderr
    # And it really needs them: one changed context line and it no longer applies.
    (tmp_path / _FILE).write_text(_pre_image(text).replace("// Launch async tasks", "// launch async tasks"))
    assert _apply_check(tmp_path).returncode != 0


@pytest.mark.skipif(not os.environ.get("TTDEV_RAMP_PATCH_TT_METAL"), reason="no tt-metal checkout at the pin")
def test_ramp_patch_applies_to_the_pinned_tt_metal():
    res = _apply_check(Path(os.environ["TTDEV_RAMP_PATCH_TT_METAL"]))
    assert res.returncode == 0, res.stderr


def test_ramp_knobs_default_to_one_chip_and_250_ms():
    added = "\n".join(ln[1:] for h in _hunks(_PATCH.read_text()) for ln in h if ln[0] == "+")
    assert 'ramp_env("TTDEV_FABRIC_RAMP_CHIPS", 1)' in added
    assert 'ramp_env("TTDEV_FABRIC_RAMP_GAP_MS", 250)' in added
    assert set(re.findall(r"getenv\((\w+)\)", added)) == {"name"}, "no other knob"


def test_ramp_changes_only_when_each_chip_starts_not_what_is_checked():
    removed = [ln[1:].strip() for h in _hunks(_PATCH.read_text()) for ln in h if ln[0] == "-"]
    # The one line it replaces is the launch loop; every chip's workload is still launched.
    assert removed == ["for (auto& [device_id, mesh_workload] : mesh_workloads) {"]
    added = "\n".join(ln[1:] for h in _hunks(_PATCH.read_text()) for ln in h if ln[0] == "+")
    assert "launch_order.push_back(device_id)" in added and "mesh_workloads.at(device_id)" in added
    # Nothing that decides the verdict or the link list is touched.
    for name in ("hard_fail", "validate_", "link_metrics", "generate_", "num_iterations", "timeout"):
        assert name not in added, name


def test_broker_passes_the_ramp_knobs_through_and_keeps_the_pass_rule(tmp_path, monkeypatch):
    current = tmp_path / "current"
    binary = current / "build/tools/scaleout/run_cluster_validation"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    for var in (
        "TT_DEVICE_MCP_FABRIC_CHECK_CMD",
        "TTDEV_FABRIC_BIN",
        "TTDEV_FABRIC_RUNTIME_ROOT",
        "TTDEV_FABRIC_DESCRIPTOR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("TTDEV_FABRIC_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("TTDEV_FABRIC_RAMP_CHIPS", raising=False)
    monkeypatch.delenv("TTDEV_FABRIC_RAMP_GAP_MS", raising=False)
    argv, env = fabric.build_command()
    assert argv[0] == str(binary) and argv[3:] == ["--hard-fail", "--send-traffic", "--num-iterations", "1"]
    assert "TTDEV_FABRIC_RAMP_CHIPS" not in env and "TTDEV_FABRIC_RAMP_GAP_MS" not in env, "the validator's defaults"
    monkeypatch.setenv("TTDEV_FABRIC_RAMP_CHIPS", "2")
    monkeypatch.setenv("TTDEV_FABRIC_RAMP_GAP_MS", "0")
    _, env = fabric.build_command()
    assert env["TTDEV_FABRIC_RAMP_CHIPS"] == "2" and env["TTDEV_FABRIC_RAMP_GAP_MS"] == "0"
