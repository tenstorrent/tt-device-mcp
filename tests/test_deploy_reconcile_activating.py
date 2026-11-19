# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""reconcile leaves a broker that is already starting alone (spec 08 I19).

The broker sits in `activating` through its startup check, which can take minutes after a boot.
reconcile used to read any state but `active` as down and run `reset-failed` + `start`, which only
blocks behind the start job already running and logs an intervention that never happened. These
run step 1 of the script alone, against a stub `systemctl`, so nothing on the host is touched.
"""

import subprocess
from pathlib import Path

import pytest

_RECONCILE = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-reconcile.sh"


def _step1(tmp_path: Path) -> str:
    lines = _RECONCILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# 1) Broker service"))
    end = next(i for i, line in enumerate(lines) if line.startswith("# 1b)"))
    block = "\n".join(lines[start:end])
    assert "/run/tt-device-broker/reconcile_inprogress" in block
    return block.replace("/run/tt-device-broker/reconcile_inprogress", str(tmp_path / "inprogress"))


def _run(tmp_path: Path, state: str) -> tuple[list[str], str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        f'if [ "$1" = is-active ]; then echo "{state}"; [ "{state}" = active ] && exit 0; exit 3; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o700)
    script = tmp_path / "step1.sh"
    script.write_text("set -u\n" + _step1(tmp_path) + "\n")
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    made = calls.read_text().splitlines() if calls.exists() else []
    return made, r.stderr


@pytest.mark.parametrize("state", ["activating", "reloading", "deactivating"])
def test_an_in_progress_broker_is_not_reset_or_started(tmp_path, state):
    made, err = _run(tmp_path, state)
    assert made == ["is-active tt-device-broker"], f"reconcile intervened in a {state} broker: {made}"
    assert "in progress" in err and "resetting" not in err


def test_an_in_progress_broker_logs_once_per_episode(tmp_path):
    _, first = _run(tmp_path, "activating")
    _, second = _run(tmp_path, "activating")
    assert first.count("in progress") == 1
    assert second == "", "a broker still starting was logged again on the next lap"
    # Once it reaches active, the next start episode logs again.
    _run(tmp_path, "active")
    _, again = _run(tmp_path, "activating")
    assert again.count("in progress") == 1


@pytest.mark.parametrize("state", ["failed", "inactive", ""])
def test_a_down_broker_is_still_reset_and_started(tmp_path, state):
    made, err = _run(tmp_path, state)
    assert "reset-failed tt-device-broker" in made and "start tt-device-broker" in made
    assert "resetting + starting" in err


def test_an_active_broker_is_left_alone(tmp_path):
    made, err = _run(tmp_path, "active")
    assert made == ["is-active tt-device-broker"] and err == ""
