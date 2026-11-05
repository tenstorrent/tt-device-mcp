# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A crashed dispatch probe keeps its what() text in the verdict, and its output tail on stderr.

"terminate called after throwing ..." sits above "what(): <cause>", so a first-match grep kept the
exception type and dropped the cause. Runs the wrapper against stand-in binaries: no device is opened.
"""

import pytest

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


# The measured shape: metal threw, its abort handler printed, and teardown hung until the cap's SIGKILL.
_CRASH_THEN_HANG = (
    "TT_THROW @ tt_metal/llrt/llrt.cpp:566: tt::exception\n"
    "info:\n"
    "Device 3: Timeout waiting for physical cores to finish\n"
    "*** End of error message ***\n"
)


def _killed_at_cap(tmp_path, body: str):
    """A stand-in probe that prints ``body`` and is then SIGKILLed, as the cap's ``timeout --signal=KILL`` does."""
    p = tmp_path / "probe"
    out = tmp_path / "probe-output.txt"
    out.write_text(body)
    p.write_text(f"#!/bin/sh\ncat '{out}'\nkill -KILL $$\n")
    p.chmod(0o755)
    return {"TTDEV_DISPATCH_BIN": str(p)}


@pytest.mark.parametrize(
    "body",
    [
        _CRASH_THEN_HANG,
        "*** End of error message ***\n",
        "terminate called after throwing an instance of 'std::runtime_error'\n  what():  bad core\n",
        "TT_FATAL @ fabric.cpp:173: forwarding_direction.has_value()\n",
        "Aborted (core dumped)\n",
    ],
)
def test_a_crash_killed_at_the_cap_is_a_dispatch_failure(tmp_path, body):
    """A crash is a measured failure even when teardown then hangs into the cap: exit 1, never 77."""
    r = _run_wrapper(tmp_path, "ok\n", 0, **_killed_at_cap(tmp_path, body))
    assert r.returncode == 1, r.stderr
    assert "crashed, then hung until its 90s cap killed it" in r.stderr
    verdict = [ln for ln in r.stderr.splitlines() if "dispatch FAILED" in ln][-1]
    assert "(rc=137)" in verdict
    assert "CANNOT CHECK" not in r.stderr


def test_a_crash_killed_at_the_cap_names_the_throw(tmp_path):
    r = _run_wrapper(tmp_path, "ok\n", 0, **_killed_at_cap(tmp_path, _CRASH_THEN_HANG))
    verdict = [ln for ln in r.stderr.splitlines() if "dispatch FAILED" in ln][-1]
    assert verdict.endswith("(rc=137): TT_THROW @ tt_metal/llrt/llrt.cpp:566: tt::exception")


def test_a_plain_cap_kill_with_no_crash_stays_cannot_check(tmp_path):
    """A probe that was only slow measured nothing: the cap kill keeps its 77 (spec 04 I84)."""
    body = "Opening mesh device 8x4\nDispatching program to 32 devices\n"
    r = _run_wrapper(tmp_path, "ok\n", 0, **_killed_at_cap(tmp_path, body))
    assert r.returncode == 77, r.stderr
    assert "dispatch probe hit its 90s cap without a verdict" in r.stderr
    assert "dispatch FAILED" not in r.stderr


def test_a_tt_throw_exit_134_is_a_dispatch_failure(tmp_path):
    r = _run_wrapper(tmp_path, "ok\n", 0, **_probe(tmp_path, _CRASH_THEN_HANG))
    assert r.returncode == 1, r.stderr
    verdict = [ln for ln in r.stderr.splitlines() if "dispatch FAILED" in ln][-1]
    assert verdict.endswith("(rc=134): TT_THROW @ tt_metal/llrt/llrt.cpp:566: tt::exception")
