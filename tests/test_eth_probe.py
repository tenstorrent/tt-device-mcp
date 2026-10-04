# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""``health.monitors.eth``: argv/env resolution and exit-code classification for the passive
eth-heartbeat pre-read, replacing what used to be `tt-device-eth-heartbeat-check.sh` (see
`test_eth_heartbeat_check_wrapper.py`, retired alongside the wrapper it pinned).

The invariant that carries the most weight, ported verbatim from the wrapper: only the
probe's deliberate frozen sentinel (exit 3) may read as unhealthy. A probe that merely
crashed (1), timed out (124), or was OOM-killed (137) is a non-verdict and must launder to a
skip — mapping any of those to a hold would strand a healthy mesh on a reader bug. The other
invariant is the DO-NOT-WIRE self-block: `build()` must refuse to hand back a runnable
built-in command until `TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN=1` acknowledges the device-attach
has been validated on a reserved box, however good the resolved python/probe look.

Every test here uses a scripted stand-in for both the python and the probe — no real
ttexalens, no device — via `tests/conftest.py`'s `isolate_device_state`, which also sandboxes
every env var this module reads.
"""

import os
import sys
from pathlib import Path

import pytest

from tt_device_mcp.health.monitors import eth
from tt_device_mcp.health.monitors.eth import (
    build,
    build_reason,
    check,
    classify_exit,
    probe_timeout_sec,
    resolve_python,
)

# A stand-in for a ttexalens python. `-c import ttexalens` honours FAKE_IMPORT_RC so
# resolve_python's import-check branch is drivable off-box; run as `python probe.py` it exits
# FAKE_PROBE_RC, letting a full build()+check() pass exercise the exit-laundering table without
# a real probe. FAKE_IMPORT_RC is a process-wide env var, so it only distinguishes candidates in
# tests that only ever have ONE stub python in play at a time; a test that needs two candidates
# with DIFFERENT import outcomes writes its own fixed-outcome stub instead (see
# `_fake_python_fixed`) so the two cannot collide on the same env var.
_FAKE_PY = """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then exit "${FAKE_IMPORT_RC:-0}"; fi
exit "${FAKE_PROBE_RC:-0}"
"""


def _fake_python(tmp_path: Path, name: str = "fakepy") -> Path:
    py = tmp_path / name
    py.write_text(_FAKE_PY)
    py.chmod(0o755)
    return py


def _fake_python_fixed(tmp_path: Path, name: str, *, import_ok: bool) -> Path:
    """A stub whose import outcome is baked in, not env-driven — for a test that needs two
    candidate pythons disagreeing on importability at once, where a shared FAKE_IMPORT_RC
    could not tell them apart."""
    py = tmp_path / name
    py.write_text(f'#!/usr/bin/env bash\nif [ "$1" = "-c" ]; then exit {0 if import_ok else 1}; fi\nexit 0\n')
    py.chmod(0o755)
    return py


def _fake_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "probe.py"
    probe.write_text("# dummy; the stub python ignores its contents\n")
    return probe


# --- classify_exit: the exit-laundering table (verbatim from the wrapper's own test) --------


@pytest.mark.parametrize(
    "rc,expected_ok",
    [
        (0, True),  # all advancing
        (3, False),  # the probe's deliberate FROZEN sentinel
        (1, None),  # bare crash must NEVER read as frozen
        (77, None),  # probe's own cannot-check
        (124, None),  # timeout SIGKILL
        (137, None),  # OOM
        (139, None),  # segfault
    ],
)
def test_exit_laundering(rc, expected_ok):
    ok, _ = classify_exit(rc)
    assert ok is expected_ok


def test_hung_read_is_skipped_not_frozen():
    # rc=None here is run_probe's OWN bound expiring (see eth.check/probe_timeout_sec), not the
    # caller's outer timeout — that is CANNOT_CHECK, exactly like the probe's own 77, never a
    # verdict this module invents. The broker-side outer hang is a SEPARATE case handled in
    # monitor.verify_eth_heartbeat, which never reaches classify_exit at all for that path.
    ok, _ = classify_exit(None)
    assert ok is None


# --- resolve_python: deterministic candidates, never a glob into a user's home --------------


def test_no_candidates_resolves_nothing(tmp_path, monkeypatch):
    # Sandbox floor: nothing set, and TTDEV_VALIDATOR_ROOT (from isolate_device_state) has no
    # current/python_env either.
    assert resolve_python() is None


def test_operator_pin_that_imports_resolves(tmp_path, monkeypatch):
    # The pin's own path carries no checkout root (it is not a `.../python_env/bin/python`
    # layout), so the tree falls back to the validator's current/ — which has to exist, exactly
    # as the pin's fallback tree would need to on a real host.
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    py = _fake_python(tmp_path)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(py))
    resolved = resolve_python()
    assert resolved is not None
    python, tree = resolved
    assert python == str(py)
    assert tree == str(vroot / "current")


def test_operator_pin_that_cannot_import_is_skipped(tmp_path, monkeypatch):
    py = _fake_python(tmp_path)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(py))
    monkeypatch.setenv("FAKE_IMPORT_RC", "1")
    assert resolve_python() is None


def test_non_executable_candidate_is_skipped(tmp_path, monkeypatch):
    py = tmp_path / "notexec"
    py.write_text(_FAKE_PY)  # deliberately no chmod +x
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(py))
    assert resolve_python() is None


def test_validator_python_env_resolves_to_current(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    current = vroot / "current"
    py_dir = current / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    py = py_dir / "python"
    py.write_text(_FAKE_PY)
    py.chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    python, tree = resolve_python()
    assert python == str(py)
    assert tree == str(current)


def test_eth_venv_env_var_relocates_the_venv_candidate(tmp_path, monkeypatch):
    # TTDEV_ETH_VENV moves the broker-owned eth-venv candidate — the retired wrapper honoured it,
    # so a host whose venv lives off the default path must not lose its only python to the
    # in-package move. Like the operator pin, its tree falls back to the validator's current/.
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    venv_bin = tmp_path / "elsewhere-venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text(_FAKE_PY)
    py.chmod(0o755)
    monkeypatch.setenv("TTDEV_ETH_VENV", str(tmp_path / "elsewhere-venv"))
    resolved = resolve_python()
    assert resolved is not None
    python, tree = resolved
    assert python == str(py)
    assert tree == str(vroot / "current")


def test_other_python_env_tree_infers_its_own_root(tmp_path, monkeypatch):
    # A python_env this box happens to have that is NOT the validator's own (e.g. a dev tt-metal
    # checkout's venv): ttexalens reads registers against the build ITS python came from, so the
    # tree must be inferred from that python's own path, not defaulted to the validator's current/.
    checkout = tmp_path / "some-dev-checkout"
    py_dir = checkout / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    py = py_dir / "python"
    py.write_text(_FAKE_PY)
    py.chmod(0o755)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(py))
    python, tree = resolve_python()
    assert python == str(py)
    assert tree == str(checkout)


def test_candidate_order_pin_beats_validator_env(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    current = vroot / "current"
    val_py_dir = current / "python_env" / "bin"
    val_py_dir.mkdir(parents=True)
    (val_py_dir / "python").write_text(_FAKE_PY)
    (val_py_dir / "python").chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))

    pin = _fake_python(tmp_path, name="pinned")
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))

    python, _ = resolve_python()
    assert python == str(pin)


def test_falls_through_a_broken_pin_to_the_validator_env(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    current = vroot / "current"
    val_py_dir = current / "python_env" / "bin"
    val_py_dir.mkdir(parents=True)
    val_py = val_py_dir / "python"
    val_py.write_text(_FAKE_PY)  # this one imports fine (FAKE_IMPORT_RC unset -> 0)
    val_py.chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))

    # The pin exists and is executable, but cannot import ttexalens (a FIXED outcome, not
    # env-driven, so it does not also break the validator's own python above) —
    # resolve_python must not stop there; it falls through to the next candidate.
    pin = _fake_python_fixed(tmp_path, "pinned", import_ok=False)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))

    python, tree = resolve_python()
    assert python == str(val_py)
    assert tree == str(current)


# --- resolve_python's per-process cache: the import checks run once, not every gate ----------
#
# On an armed host build() runs on every clean post-job gate, and the import checks it would
# spawn there (up to 3 x 10s) sit outside the read's own bound. A counting stub python records
# each `-c import ttexalens` it answers, so these tests count spawns, not just results.


def _counting_python(path: Path, log: Path, *, import_ok: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "-c" ]; then echo x >> "{log}"; exit {0 if import_ok else 1}; fi\n'
        "exit 0\n"
    )
    path.chmod(0o700)
    return path


def _spawns(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def _validator_release(vroot: Path, name: str, log: Path) -> Path:
    """A release dir with its own python_env, as the validator pipeline stages one."""
    release = vroot / name
    _counting_python(release / "python_env" / "bin" / "python", log)
    return release


def test_resolved_python_is_cached_across_calls(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    log = tmp_path / "spawns"
    release = _validator_release(vroot, "r1", log)
    (vroot / "current").symlink_to(release)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))

    first = resolve_python()
    assert first is not None
    assert _spawns(log) == 1
    for _ in range(5):
        assert resolve_python() == first
    assert _spawns(log) == 1, "a cached python re-ran its import check"


def test_build_on_every_gate_spawns_the_import_check_once(tmp_path, monkeypatch):
    # The gate's actual call: build() on an armed host, once per clean post-job pass.
    vroot = tmp_path / "validator"
    log = tmp_path / "spawns"
    release = _validator_release(vroot, "r1", log)
    (vroot / "current").symlink_to(release)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")
    monkeypatch.setenv("TTDEV_ETH_CHECK_PROBE", str(_fake_probe(tmp_path)))
    monkeypatch.setenv("TTDEV_ETH_CHECK_CACHE", str(tmp_path / "cache"))

    for _ in range(4):
        assert build() is not None
    assert _spawns(log) == 1


def test_repointing_current_re_resolves(tmp_path, monkeypatch):
    # The validator pipeline flips `current` to a new release; the cached python belongs to the
    # old one and must not outlive the flip.
    vroot = tmp_path / "validator"
    log = tmp_path / "spawns"
    r1 = _validator_release(vroot, "r1", log)
    r2 = _validator_release(vroot, "r2", log)
    current = vroot / "current"
    current.symlink_to(r1)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))

    assert resolve_python() is not None
    assert _spawns(log) == 1
    current.unlink()
    current.symlink_to(r2)
    assert resolve_python() is not None
    assert _spawns(log) == 2, "a re-pointed current/ kept the old release's cached answer"
    assert resolve_python() is not None
    assert _spawns(log) == 2


def test_a_changed_pin_re_resolves(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    log = tmp_path / "spawns"
    a = _counting_python(tmp_path / "a" / "python", log)
    b = _counting_python(tmp_path / "b" / "python", log)

    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(a))
    assert resolve_python()[0] == str(a)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(b))
    assert resolve_python()[0] == str(b)
    assert _spawns(log) == 2


def test_a_cached_python_that_disappears_re_resolves(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    log = tmp_path / "spawns"
    pin = _counting_python(tmp_path / "pin" / "python", log)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))

    assert resolve_python()[0] == str(pin)
    pin.unlink()
    assert resolve_python() is None


def test_no_python_is_cached_only_for_the_negative_ttl(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    log = tmp_path / "spawns"
    pin = _counting_python(tmp_path / "pin" / "python", log, import_ok=False)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))

    assert resolve_python() is None
    assert resolve_python() is None
    assert _spawns(log) == 1
    # ttexalens gets installed into the pinned python; the miss expires and it is picked up.
    _counting_python(pin, log, import_ok=True)
    key, answer, at = eth._python_cache
    monkeypatch.setattr(eth, "_python_cache", (key, answer, at - eth.NEGATIVE_TTL_SEC - 1))
    assert resolve_python()[0] == str(pin)
    assert _spawns(log) == 2


def test_an_import_check_timeout_is_not_cached_as_no_python(tmp_path, monkeypatch):
    # A slow import (cold page cache, slow NFS) is not proof there is no python: the next gate
    # must try again rather than skip the read as not_configured for NEGATIVE_TTL_SEC.
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    log = tmp_path / "spawns"
    pin = _counting_python(tmp_path / "pin" / "python", log)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))
    pin.write_text(f'#!/usr/bin/env bash\necho x >> "{log}"\nsleep 5\n')

    assert resolve_python(import_timeout_sec=0.2) is None
    _counting_python(pin, log)
    assert resolve_python() is not None
    assert _spawns(log) == 2


def test_forget_python_forces_a_fresh_import_check(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    (vroot / "current").mkdir(parents=True)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    log = tmp_path / "spawns"
    pin = _counting_python(tmp_path / "pin" / "python", log)
    monkeypatch.setenv("TTDEV_ETH_CHECK_PYTHON", str(pin))

    assert resolve_python() is not None
    eth.forget_python()
    # ttexalens vanished from the venv in place: same path, same current/.
    _counting_python(pin, log, import_ok=False)
    assert resolve_python() is None
    assert _spawns(log) == 2


# --- probe_timeout_sec: TTDEV_ETH_CHECK_TIMEOUT, kept below the caller's own bound -----------


def test_probe_timeout_default():
    assert probe_timeout_sec() == 45.0


def test_probe_timeout_env_override(monkeypatch):
    monkeypatch.setenv("TTDEV_ETH_CHECK_TIMEOUT", "12.5")
    assert probe_timeout_sec() == 12.5


def test_probe_timeout_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TTDEV_ETH_CHECK_TIMEOUT", "not-a-number")
    assert probe_timeout_sec(default=45.0) == 45.0


# --- build(): the DO-NOT-WIRE self-block, preserved verbatim --------------------------------


def _clear_it(tmp_path, monkeypatch):
    """A validator tree + a resolvable python + the probe present — everything a WIRED,
    validated host would have. Used to prove the self-block alone (not a missing artifact)
    is what withholds a verdict."""
    vroot = tmp_path / "validator"
    current = vroot / "current"
    py_dir = current / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    (py_dir / "python").write_text(_FAKE_PY)
    (py_dir / "python").chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    install_root = tmp_path / "install-root"  # $TTDEV_ROOT: install-tt-device-broker.sh's layout
    install_root.mkdir()
    monkeypatch.setenv("TTDEV_ROOT", str(install_root))
    probe = install_root / "eth-heartbeat-probe.py"
    probe.write_text("# dummy\n")
    return current


def test_self_block_withholds_even_with_everything_else_ready(tmp_path, monkeypatch):
    _clear_it(tmp_path, monkeypatch)
    # TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN is unset (isolate_device_state's floor) — the wiring
    # alone must never turn on a probe that has never run on hardware.
    assert build() is None


def test_self_block_cleared_with_everything_ready_builds_a_command(tmp_path, monkeypatch):
    current = _clear_it(tmp_path, monkeypatch)
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    # The real default (/var/cache/...) is root-owned; point it at a tmp dir so the
    # mkdir this test asserts on does not depend on running as root.
    monkeypatch.setenv("TTDEV_ETH_CHECK_CACHE", str(tmp_path / "cache"))
    built = build()
    assert built is not None
    argv, env = built
    assert argv[0].endswith("python_env/bin/python")
    assert argv[1].endswith("eth-heartbeat-probe.py")
    assert env["TT_METAL_HOME"] == str(current)
    assert env["TT_METAL_RUNTIME_ROOT"] == str(current)
    assert Path(env["TT_METAL_CACHE"]).is_dir()


def test_self_block_cleared_but_no_python_is_still_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    # No validator python_env, no eth-venv (patched to a nonexistent path by conftest), no pin.
    assert build() is None


def test_self_block_cleared_but_no_probe_is_still_none(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    current = vroot / "current"
    py_dir = current / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    (py_dir / "python").write_text(_FAKE_PY)
    (py_dir / "python").chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    # No probe staged anywhere findable (no sibling, no TTDEV_ETH_CHECK_PROBE).
    assert build() is None


def test_probe_override_path_used_when_set(tmp_path, monkeypatch):
    _clear_it(tmp_path, monkeypatch)
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    override_probe = tmp_path / "elsewhere" / "my-probe.py"
    override_probe.parent.mkdir()
    override_probe.write_text("# dummy\n")
    monkeypatch.setenv("TTDEV_ETH_CHECK_PROBE", str(override_probe))
    argv, _ = build()
    assert argv[1] == str(override_probe)


def test_operator_cmd_wins_regardless_of_artifacts(tmp_path, monkeypatch):
    # No validator, no python, no probe — none of that matters once an operator has named their
    # own command. Armed, because arming gates the override too (a disarmed override is pinned
    # in tests/test_rung_arming.py): the self-test times every reader, custom or built-in.
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "/opt/whatever/my-check.sh")
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")
    argv, env = build()
    assert argv == ["/bin/bash", "-c", "/opt/whatever/my-check.sh"]
    assert env["PYTHONUNBUFFERED"] == "1"


def test_probe_falls_back_to_the_repo_checkout_when_not_installed(tmp_path, monkeypatch):
    # A dev running straight out of a checkout has no $TTDEV_ROOT install (sandboxed to an
    # empty dir by isolate_device_state) — _probe_path() must still find the repo's own
    # deploy/tt-device-eth-heartbeat-probe.py, mirroring the retired wrapper's own
    # search-next-to-itself for its un-renamed, in-repo name.
    vroot = tmp_path / "validator"
    py_dir = vroot / "current" / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    (py_dir / "python").write_text(_FAKE_PY)
    (py_dir / "python").chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")

    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    repo_probe = checkout / "deploy" / "tt-device-eth-heartbeat-probe.py"
    repo_probe.write_text("# dummy\n")
    monkeypatch.setattr(eth, "_REPO_ROOT", checkout)

    argv, _ = build()
    assert argv[1] == str(repo_probe)


# --- build_reason(): the specific cause, not just a generic skip ----------------------------


def test_build_reason_names_the_unarmed_rung(tmp_path, monkeypatch):
    # Everything else ready (python, probe); only the arming gate withholds it. The rung is armed
    # per host by the broker's own startup self-test, so that is what an unarmed host must be told.
    _clear_it(tmp_path, monkeypatch)
    reason = build_reason()
    assert "not armed" in reason
    assert "self-test" in reason


def test_build_reason_names_the_missing_python(monkeypatch):
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    reason = build_reason()
    assert "ttexalens" in reason
    assert "TTDEV_ETH_CHECK_PYTHON" in reason  # the fix command an operator needs


def test_build_reason_names_the_missing_probe(tmp_path, monkeypatch):
    vroot = tmp_path / "validator"
    py_dir = vroot / "current" / "python_env" / "bin"
    py_dir.mkdir(parents=True)
    (py_dir / "python").write_text(_FAKE_PY)
    (py_dir / "python").chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(vroot))
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    reason = build_reason()
    assert "probe" in reason.lower()


def test_build_reason_matches_build_none_in_practice(tmp_path, monkeypatch):
    # build() and build_reason() must agree: whenever the former is None, the latter must
    # actually explain something, not silently claim "not available" while a real cause exists.
    _clear_it(tmp_path, monkeypatch)
    assert build() is None
    assert build_reason() != "not available"


# --- check(): the shared subproc runner's plumbing, decoded/stripped -------------------------


@pytest.mark.asyncio
async def test_check_runs_argv_and_returns_decoded_output():
    rc, text = await check(
        [sys.executable, "-c", "print('all active-eth cores advancing')"],
        {**os.environ},
        timeout_sec=5,
        track=lambda _p: None,
    )
    assert rc == 0
    assert text == "all active-eth cores advancing"


@pytest.mark.asyncio
async def test_check_timeout_returns_none_rc():
    rc, _ = await check(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        {**os.environ},
        timeout_sec=0.2,
        track=lambda _p: None,
    )
    assert rc is None


# --- end-to-end: build() -> check() -> classify_exit(), the whole retired wrapper's job -------


@pytest.mark.parametrize(
    "probe_rc,expected_ok",
    [
        (0, True),
        (3, False),
        (1, None),
        (77, None),
        (124, None),
        (137, None),
        (139, None),
    ],
)
@pytest.mark.asyncio
async def test_end_to_end_probe_exit_maps_to_the_same_verdict(tmp_path, monkeypatch, probe_rc, expected_ok):
    """The whole pipeline the wrapper used to run in bash, now in-package: resolve, spawn,
    classify. Exercised through the SAME exit codes as `test_exit_laundering` to prove the
    plumbing (build -> check -> classify_exit) preserves the table end to end, not just in
    the pure classifier."""
    current = _clear_it(tmp_path, monkeypatch)
    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    monkeypatch.setenv("FAKE_PROBE_RC", str(probe_rc))

    argv, env = build()
    rc, _ = await check(argv, env, timeout_sec=5, track=lambda _p: None, cwd=str(current))
    ok, _ = classify_exit(rc)
    assert ok is expected_ok
