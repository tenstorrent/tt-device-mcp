# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The passive active-eth-core heartbeat pre-read: argv/env resolution + exit-code
classification for ``deploy/tt-device-eth-heartbeat-probe.py``, which stays a subprocess
(it imports ``ttexalens``, a native package tt-metal builds, never the broker's own venv).

This used to be a bash wrapper (``tt-device-eth-heartbeat-check.sh``) with the same job
``health.monitors.fabric`` does for the traffic pass: resolve a runnable command, then fold
its exit into a verdict the gate can act on. It is retired in favor of this module, following
the same split ``fabric`` already established — ``build()`` resolves argv/env (or ``None``
when nothing is runnable), ``classify_exit()`` turns a raw exit code into ``(ok, detail)``,
and ``monitor.HealthMonitor.verify_eth_heartbeat`` is the orchestrator that runs one through
``subproc.run_probe`` and applies the other, exactly mirroring ``verify_fabric_health``.

Two things this module must get exactly right, because getting them wrong strands a healthy
mesh or, worse, hides a wedge:

* The probe signals FROZEN with a distinct exit (3), not 1, because a Python process that
  dies on an unhandled exception also exits 1 — a crash and a verdict must not share a code.
  ``classify_exit`` maps ONLY exit 3 to unhealthy; every other non-zero code (a bare crash,
  the probe's own 77, a segfault, an OOM kill) is "the read did not complete", never frozen.
* The rung is ARMED PER HOST, AUTOMATICALLY: the broker times the attach once at startup and
  sets ``TTDEV_ETH_CHECK_ARMED=1`` for the process when it answers inside budget; otherwise the
  rung stays off and says so in the journal. Until then ``build()`` refuses to hand back a
  runnable built-in command at all — wiring ``TT_DEVICE_MCP_ETH_HEARTBEAT_CMD`` alone, or simply
  upgrading this package, must never turn on a probe that has never run on this hardware.
  ``TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN`` is the pre-rename spelling and is still honoured, so a
  host armed by hand under the old name does not silently disarm on upgrade.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from tt_device_mcp.health.monitors.subproc import Track, run_probe

# The probe's deliberate frozen sentinel (see tt-device-eth-heartbeat-probe.py). A crash exits
# 1, which must never be mistaken for this.
_PROBE_FROZEN = 3

# The hardcoded last-resort python candidate. Not overridable by env — TTDEV_ETH_CHECK_PYTHON
# is the portable, operator-controlled mechanism — so it is a module attribute (not inlined)
# purely so tests can point it at a path that cannot exist, rather than relying on this box
# never happening to have a real eth-venv staged at this exact path.
DEFAULT_ETH_VENV_PYTHON = "/opt/tt-device-broker/eth-venv/bin/python"

# This package's own checkout, for the dev-checkout probe fallback in `_probe_path()` — a
# module attribute (not inlined) for the same reason DEFAULT_ETH_VENV_PYTHON is one: an
# editable install (this repo's own test suite) genuinely has `deploy/` alongside this file, so
# an unsandboxed test would resolve to the real probe script instead of a scripted stand-in.
_REPO_ROOT = Path(__file__).resolve().parents[4]

# resolve_python() spawns up to three `python -c "import ttexalens"` checks (10s each), and on an
# armed host build() calls it on every clean post-job gate — outside the read's own bound. So the
# answer is cached per process, keyed on the candidate list and where the validator's `current`
# symlink points: re-pointing `current`, or changing any candidate env var, re-resolves. A hit is
# re-checked with cheap stat calls only (the python still executable, its tree still there). A
# miss (None) is cached for NEGATIVE_TTL_SEC only, so a venv provisioned later is picked up
# without a restart. forget_python() drops the entry when a read reaches no verdict.
NEGATIVE_TTL_SEC = 60.0
_python_cache: dict[tuple, tuple[Optional[tuple[str, str]], float]] = {}
_python_cache_lock = threading.Lock()


def _tree_for(python: str, current: str) -> str:
    """The tt-metal checkout ``python`` was built from — THAT tree, not the validator's,
    because ttexalens reads registers against the build its python came from.

    Only a ``.../python_env/bin/python`` layout carries its own checkout root in its path (two
    levels up from ``bin/python``), and only when it is not the validator's OWN python_env
    (which already resolves to ``current``). Every other candidate — the validator's own, an
    eth-venv, an arbitrary operator pin — has no such tree encoded in its path, so it falls
    back to the validator's ``current/``.
    """
    if python != f"{current}/python_env/bin/python" and python.endswith("/python_env/bin/python"):
        return str(Path(python).parent.parent.parent)
    return current


def resolve_python(*, import_timeout_sec: float = 10.0) -> Optional[tuple[str, str]]:
    """The first candidate python that can ``import ttexalens``, as ``(python, tt_metal_tree)``.

    Candidates, in order — deterministic and controlled, never a glob into a user's home (that
    would make the reader depend on some random dev's tree):
      1. ``TTDEV_ETH_CHECK_PYTHON`` — the portable, operator-pinned mechanism.
      2. ``$TTDEV_VALIDATOR_ROOT/current/python_env/bin/python`` — the pinned validator's own env.
      3. a broker-owned eth-venv, if one was provisioned — at ``TTDEV_ETH_VENV`` (the retired
         wrapper honoured it, so a host that relocated its venv must not lose its python to the
         in-package move) or the default install path.
    ``None`` if no candidate both exists and can import it — a check that cannot read must
    never guess.

    Cached per process (see ``_python_cache``): only the first call, or the first after
    ``current`` is re-pointed, a candidate env var changes, a cached python or tree disappears,
    a ``None`` ages past ``NEGATIVE_TTL_SEC`` or ``forget_python()`` runs, spawns anything.
    """
    vroot = os.environ.get("TTDEV_VALIDATOR_ROOT", "/opt/tt-device-broker/validator").strip()
    current = f"{vroot}/current"
    eth_venv = os.environ.get("TTDEV_ETH_VENV", "").strip()
    candidates = [
        c
        for c in (
            os.environ.get("TTDEV_ETH_CHECK_PYTHON", "").strip(),
            f"{current}/python_env/bin/python",
            f"{eth_venv}/bin/python" if eth_venv else DEFAULT_ETH_VENV_PYTHON,
        )
        if c
    ]

    key = (tuple(candidates), os.path.realpath(current))
    with _python_cache_lock:
        hit = _python_cache.get(key)
        if hit is not None:
            resolved, at = hit
            if resolved is None:
                if time.monotonic() - at < NEGATIVE_TTL_SEC:
                    return None
            elif os.access(resolved[0], os.X_OK) and os.path.isdir(resolved[1]):
                return resolved
        resolved = _resolve_python_uncached(candidates, current, import_timeout_sec)
        _python_cache.clear()
        _python_cache[key] = (resolved, time.monotonic())
        return resolved


def forget_python() -> None:
    """Drop the cached ``resolve_python()`` answer, so the next ``build()`` re-runs the import
    checks — called when a read reaches no verdict, which a python that lost ttexalens in place
    (``current`` unchanged) would cause."""
    with _python_cache_lock:
        _python_cache.clear()


def _resolve_python_uncached(
    candidates: list[str], current: str, import_timeout_sec: float
) -> Optional[tuple[str, str]]:
    for python in candidates:
        if not os.access(python, os.X_OK):
            continue
        try:
            imported = (
                subprocess.run(
                    [python, "-c", "import ttexalens"],
                    capture_output=True,
                    timeout=import_timeout_sec,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not imported:
            continue
        tree = _tree_for(python, current)
        if os.path.isdir(tree):
            return python, tree
    return None


def _probe_path() -> Optional[str]:
    """The probe script to run, in order:
      1. ``TTDEV_ETH_CHECK_PROBE`` — an explicit override.
      2. ``$TTDEV_ROOT/eth-heartbeat-probe.py`` — where ``install-tt-device-broker.sh`` actually
         stages it. ``TTDEV_ROOT`` (not ``TTDEV_VALIDATOR_ROOT``, a different, independently
         overridable root) is the broker's own install root — the same variable
         ``apply-host-config.sh`` reads for it.
      3. this package's own checked-out ``deploy/tt-device-eth-heartbeat-probe.py`` — a dev
         running straight out of a repo checkout has no ``$TTDEV_ROOT`` install at all. Mirrors
         the retired wrapper's own two-candidate search next to itself (the installed name and
         the repo's un-renamed one).
    Readable, not merely present (``-r``, matching the wrapper's own test) — a file that exists
    but cannot be read could not have been run either.
    """
    override = os.environ.get("TTDEV_ETH_CHECK_PROBE", "").strip()
    if override:
        return override if os.access(override, os.R_OK) else None
    root = os.environ.get("TTDEV_ROOT", "/opt/tt-device-broker").strip()
    installed = Path(root) / "eth-heartbeat-probe.py"
    if os.access(installed, os.R_OK):
        return str(installed)
    checkout = _REPO_ROOT / "deploy" / "tt-device-eth-heartbeat-probe.py"
    return str(checkout) if os.access(checkout, os.R_OK) else None


def probe_timeout_sec(default: float = 45.0) -> float:
    """The bound on the probe run itself, ``TTDEV_ETH_CHECK_TIMEOUT`` or ``default``. Kept
    below the caller's own timeout (see ``monitor.verify_eth_heartbeat``) so a merely slow
    read still exits on its own — and folds through ``classify_exit`` — instead of being
    caught by the caller's outer bound, whose timeout is FROZEN evidence, not a skip."""
    raw = os.environ.get("TTDEV_ETH_CHECK_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def armed() -> bool:
    """Whether the eth-heartbeat rung is armed on this host.

    ``TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN`` is the pre-rename spelling, still honoured so a host
    an operator armed by hand does not silently disarm on upgrade."""
    for var in ("TTDEV_ETH_CHECK_ARMED", "TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value == "1"
    return False


def build() -> Optional[tuple[list[str], dict[str, str]]]:
    """The argv + env to run the eth-heartbeat read with, or ``None`` when nothing is
    configured/available at all. Mirrors ``fabric.build_command()``: an operator's
    ``TT_DEVICE_MCP_ETH_HEARTBEAT_CMD`` wins over the built-in probe, run through
    ``/bin/bash -c`` exactly as before; unset, this resolves the built-in probe.

    The armed gate applies to BOTH paths, exactly as the retired inline check did: the startup
    self-test times the OVERRIDE too, and its disarm (a read too slow to tell apart from a
    frozen core — whose gate-time timeout is a HOLD verdict, not a skip) must keep that reader
    from delivering verdicts, or an untimed reader that runs slow strands a healthy mesh on a
    frozen-core hold. The self-test itself arms provisionally around its one measured run, so
    this gate never starves it. (The retired wrapper enforced this for itself by reading
    ``TTDEV_ETH_CHECK_ARMED`` and exiting 77; a custom override has no such self-check, which
    is why the gate lives here and not in the command.)

    Warning for that override: point it at a real verdict-producing command, never at the raw
    ``tt-device-eth-heartbeat-probe.py`` directly. The retired wrapper was the launderer between
    the probe's exit codes and this one; without it, the override contract (exit 0 = healthy,
    anything else = unhealthy) would read the probe's own crash (exit 1) as a frozen verdict —
    exactly the forgery the sentinel exists to prevent. Use
    ``TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN=1`` (below) to run the probe itself, laundered.

    Spawns nothing and does no I/O beyond ``os.access`` checks EXCEPT ``resolve_python()``,
    which can shell out up to three candidate pythons on a cache miss — callers on an event
    loop should run this off it (e.g. ``asyncio.to_thread``), see
    ``monitor.verify_eth_heartbeat``.

    The built-in path is ``None`` — unavailable, same as no override and no validator
    installed for fabric — until ALL of: the rung is armed on this host (the broker's startup
    self-test timed the attach and set ``TTDEV_ETH_CHECK_ARMED=1``, or an operator armed it by
    hand under either spelling), a python that can ``import ttexalens`` resolves, and the probe
    script itself is found. Any one of those unmet collapses to the same "nothing to check with"
    signal callers already treat as a skip — wiring the override env var, or shipping this
    module, can never by itself turn on a read that has never run on this hardware. See
    ``build_reason()`` for WHICH of those it was.
    """
    override = os.environ.get("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "").strip()
    if override:
        if not armed():
            return None
        return ["/bin/bash", "-c", override], {**os.environ, "PYTHONUNBUFFERED": "1"}

    if not armed():
        return None
    resolved = resolve_python()
    if resolved is None:
        return None
    python, tree = resolved
    probe = _probe_path()
    if probe is None:
        return None

    # Dedicated so the (root-run) check never depends on $HOME and never pollutes a user's
    # own firmware/kernel cache — same rationale as the fabric check's TTDEV_FABRIC_CACHE.
    cache = (
        os.environ.get("TTDEV_ETH_CHECK_CACHE", "").strip()
        or "/var/cache/tt-device-broker/eth-heartbeat-tt-metal-cache"
    )
    try:
        Path(cache).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    argv = [python, probe]
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "TT_METAL_HOME": tree,
        "TT_METAL_RUNTIME_ROOT": tree,
        "TT_METAL_CACHE": cache,
    }
    return argv, env


def unavailable() -> tuple[str, str]:
    """``(journal_key, detail)`` for why ``build()`` returned ``None`` — the wrapper this module
    replaces failed loud with the exact cause AND a fix command, and callers still need that
    (the causes collapse to the same skip, but not to the same operator action).

    The key keeps the pre-split call sites' closed vocabulary: ``not_configured`` when no reader
    is wired on this host (the built-in probe's python or script is missing, and no override is
    set), ``not_armed`` when a wired reader exists but the startup self-test has not cleared the
    device-attach. The cause an operator acts on rides in the detail. Distinct keys across that
    wired/armed boundary also keep the skip journal honest when the cause flips over the process
    lifetime: each side reaches the durable record under its own dedup key.

    Wiring is judged before arming so an unwired host reads ``not_configured`` exactly as the
    pre-split code said it (arming can never fix a host with nothing to run). That means the
    ``resolve_python()`` spawn happens on unarmed gate passes too — acceptable because callers
    invoke this exclusively to explain a ``None`` from ``build()``, off the hot path, and callers
    on an event loop should still run it off-loop.
    """
    if os.environ.get("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "").strip():
        # An override builds unconditionally once armed, so reaching here means disarmed.
        return "not_armed", (
            "eth-heartbeat rung not armed on this host: the broker's startup "
            "self-test has not cleared the device-attach"
        )
    if resolve_python() is None:
        return "not_configured", (
            "no python can 'import ttexalens'. FIX: set TTDEV_ETH_CHECK_PYTHON="
            "/path/to/tt-metal/python_env/bin/python (a python that imports "
            "ttexalens), or provision a broker-owned eth-venv (TTDEV_ETH_VENV)"
        )
    if not armed():
        return "not_armed", (
            "eth-heartbeat rung not armed on this host: the broker's startup "
            "self-test has not cleared the device-attach"
        )
    if _probe_path() is None:
        return "not_configured", (
            "eth-heartbeat probe not found (set TTDEV_ETH_CHECK_PROBE, or " "stage the installed sibling)"
        )
    return "not_configured", "not available"


def build_reason() -> str:
    """The human half of :func:`unavailable`, kept for callers that only want the detail."""
    return unavailable()[1]


def classify_exit(rc: Optional[int]) -> tuple[Optional[bool], str]:
    """The probe's own exit-code contract: ``True`` healthy, ``False`` unhealthy (a reset can
    act on this — well, HOLD can; a frozen core is never reset), ``None`` skipped (nothing was
    measured).

    ``rc is None`` means ``run_probe``'s own timeout fired — the read never got to exit on its
    own within its bound — which is CANNOT_CHECK, exactly like the probe's own 77, not a
    verdict the caller invents. (The caller's OUTER timeout, wrapping this whole check, is a
    separate and deliberately different case — see ``monitor.verify_eth_heartbeat``.)

    Only the probe's deliberate frozen sentinel (3) may become ``False``. Every other
    non-zero code — a bare 1 (an unhandled python crash), a timeout SIGKILL (124), an OOM
    (137), a segfault (139), the probe's own cannot-check (77) — is "the read did not
    complete or measure", which must launder to a skip; mapping any of those to a hold would
    strand a healthy mesh on a reader bug.
    """
    if rc == 0:
        return True, "all active-eth cores advancing"
    if rc == _PROBE_FROZEN:
        return False, "a frozen active-eth core: heartbeat not advancing"
    if rc is None:
        return None, "eth-heartbeat read timed out without reaching a verdict"
    return None, f"probe exited {rc} (not a frozen verdict)"


async def check(
    argv: list[str], env: dict[str, str], *, timeout_sec: float, track: Track, cwd: Optional[str] = None
) -> tuple[Optional[int], str]:
    """Run an already-built eth-heartbeat command (see ``build()``) and return ``(rc, output)``.

    ``terminate`` is deliberately left at ``run_probe``'s own default (a hard ``killpg``): a
    hung register read has no graceful unwind worth waiting for, unlike the fabric traffic
    pass's own escalation ladder — it is either measuring or already wedged.
    """
    rc, raw = await run_probe(argv, timeout_sec=timeout_sec, track=track, stream=False, env=env, cwd=cwd)
    return rc, raw.decode("utf-8", "replace").strip()
