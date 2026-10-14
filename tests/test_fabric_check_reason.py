# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A fabric pass that exits 77 keeps the validator's first error line in its message.

An uncaught exception prints "terminate called after throwing an instance of '<type>'" on the line
above "  what():  <cause>". Every reason picker took the first match in line order, so a validator
that died on a missing 1G hugepage pool was logged with the exception type and no cause. Runs the
wrapper script against a stand-in validator: no device is opened.
"""

import subprocess
from pathlib import Path

import pytest

from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.health import monitor as health_monitor_mod
from tt_device_mcp.health.monitors.fabric import classify, first_reason

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-fabric-check.sh"

_UNCAUGHT = (
    "2026-10-08 20:44:38.1 | info | Device | Opening 32 chips\n"
    "terminate called after throwing an instance of 'tt::umd::error::UmdException<RuntimeError>'\n"
    "  what():  Hugepages are not allocated for channel: 0\n"
    "Aborted (core dumped)\n"
)
_LOGGED = (
    "2026-10-08 20:44:38.1 | info     | Device | Opening 32 chips\n"
    "2026-10-08 20:44:38.2 | critical | Always | TT_THROW @ sysmem.cpp:362: IOMMU is required\n"
    "backtrace:\n"
    " --- /opt/x/libtt_metal.so(+0x1234)\n"
)
_NO_ERROR_LINE = "banner\nsomething odd\nlast words\n"
# metal's TT_FATAL: the what() header names only the source line and the failed condition; the
# message itself comes two lines down, under "info:" (as run_cluster_validation printed it when a
# route was missing on a galaxy).
_TT_FATAL = (
    "2026-10-14 08:55:40.1 | info     | Device | Opening 32 chips\n"
    "terminate called after throwing an instance of 'std::runtime_error'\n"
    "  what():  TT_FATAL @ /opt/v/tt_metal/fabric/fabric.cpp:173: forwarding_direction.has_value()\n"
    "info:\n"
    "Could not find any forwarding direction from src (M0, D0) to dst (M0, D28)\n"
    "backtrace:\n"
    " --- /opt/v/build/lib/libtt_metal.so(+0x18ebee8)\n"
)
_TT_FATAL_REASON = (
    "what():  TT_FATAL @ /opt/v/tt_metal/fabric/fabric.cpp:173: forwarding_direction.has_value()"
    " -- Could not find any forwarding direction from src (M0, D0) to dst (M0, D28)"
)


@pytest.mark.parametrize(
    "output,expected",
    [
        (_UNCAUGHT, "what():  Hugepages are not allocated for channel: 0"),
        (_LOGGED, "| critical | Always | TT_THROW @ sysmem.cpp:362: IOMMU is required"),
        ("terminate called without an active exception\n", "terminate called without an active exception"),
        (
            "x\nfilesystem error: cannot create directory: Permission denied\n",
            "filesystem error: cannot create directory: Permission denied",
        ),
        (_NO_ERROR_LINE, None),
        (_TT_FATAL, _TT_FATAL_REASON),
        # An "info:" line that does not follow the matched line is not its message.
        ("  what():  TT_FATAL @ a.cpp:1: x\nbacktrace:\ninfo:\nunrelated\n", "what():  TT_FATAL @ a.cpp:1: x"),
    ],
)
def test_first_reason_prefers_the_cause_over_the_terminate_banner(output, expected):
    assert first_reason(output) == expected


def test_builtin_path_skip_detail_names_the_cause():
    ok, detail = classify(134, _UNCAUGHT)
    assert ok is None
    assert detail.endswith("what():  Hugepages are not allocated for channel: 0")


def test_override_path_reason_names_the_cause():
    text = _UNCAUGHT + "fabric-check: no link was tested\n"
    assert health_monitor_mod._override_reason(text) == "what():  Hugepages are not allocated for channel: 0"


def _run_wrapper(tmp_path, validator_output: str, rc: int):
    root = tmp_path / "root"
    root.mkdir()
    out = tmp_path / "validator-output.txt"
    out.write_text(validator_output)
    fake = tmp_path / "run_cluster_validation"
    fake.write_text(f"#!/bin/sh\ncat '{out}'\nexit {rc}\n")
    fake.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TTDEV_FABRIC_BIN": str(fake),
        "TTDEV_FABRIC_RUNTIME_ROOT": str(root),
        "TTDEV_FABRIC_CACHE": str(tmp_path / "cache"),
        "TTDEV_DISPATCH_BIN": str(tmp_path / "absent"),
    }
    return subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    "validator_output,cause",
    [
        (_UNCAUGHT, "what():  Hugepages are not allocated for channel: 0"),
        (_LOGGED, "TT_THROW @ sysmem.cpp:362: IOMMU is required"),
        (_NO_ERROR_LINE, "last output: last words"),
        (_TT_FATAL, _TT_FATAL_REASON),
    ],
)
def test_wrapper_77_final_line_keeps_the_validators_first_error_line(tmp_path, validator_output, cause):
    r = _run_wrapper(tmp_path, validator_output, 134)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert "validator did not complete a measurement (rc=134)" in last
    assert cause in last
    # The broker's override path quotes the same cause from the wrapper's whole output.
    if cause.startswith("last output"):
        assert health_monitor_mod._override_reason(r.stderr) == last.strip()
    else:
        assert cause in health_monitor_mod._override_reason(r.stderr)
