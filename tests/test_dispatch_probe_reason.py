# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A crashed dispatch probe keeps its what() text in the verdict, and its output tail on stderr.

"terminate called after throwing ..." sits above "what(): <cause>", so a first-match grep kept the
exception type and dropped the cause. Runs the wrapper against stand-in binaries: no device is opened.
"""

from tests.test_fabric_check_reason import _run_wrapper

_PROBE = (
    "terminate called after throwing an instance of 'tt::umd::error::UmdException<RuntimeError>'\n"
    "  what():  Timeout waiting for ARC message\n"
    "Aborted (core dumped)\n"
)


def _probe(tmp_path, body: str):
    p = tmp_path / "probe"
    out = tmp_path / "probe-output.txt"
    out.write_text(body)
    p.write_text(f"#!/bin/sh\ncat '{out}'\nexit 134\n")
    p.chmod(0o755)
    return {"TTDEV_DISPATCH_BIN": str(p)}


def test_dispatch_verdict_keeps_the_what_text(tmp_path):
    r = _run_wrapper(tmp_path, "ok\n", 0, **_probe(tmp_path, _PROBE))
    assert r.returncode == 1, r.stderr
    verdict = [ln for ln in r.stderr.splitlines() if "dispatch FAILED" in ln][-1]
    assert verdict.endswith("(rc=134): what():  Timeout waiting for ARC message")


def test_dispatch_failure_prints_the_output_tail(tmp_path):
    body = "".join(f"line {i}\n" for i in range(100)) + _PROBE
    r = _run_wrapper(tmp_path, "ok\n", 0, **_probe(tmp_path, body))
    assert r.returncode == 1, r.stderr
    assert "fabric-check|  line 0\n" in r.stderr
    assert "fabric-check|  line 99\n" in r.stderr
    assert "fabric-check|  Aborted (core dumped)\n" in r.stderr
