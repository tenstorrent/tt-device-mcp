# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The fabric traffic pass: the only check that proves the mesh actually MOVES data across
every inter-chip ethernet link, as opposed to merely enumerating (tt-smi) or reading a
passive firmware counter (the eth-heartbeat probe). It pushes one round of traffic via
tt-metal's ``run_cluster_validation`` — ~45-75s on a healthy mesh.

``TT_DEVICE_MCP_FABRIC_CHECK_CMD``, when set, names an operator's own wrapper and runs
unconditionally in its place — ``deploy/tt-device-fabric-check.sh`` stays a supported target
for it. Unset, ``build_command()`` resolves the pinned validator published under
``TTDEV_VALIDATOR_ROOT`` by ``install-fabric-validator.sh`` directly, no shell in between, so
every host without an operator override still gets the traffic check.

The decision table in ``classify()`` interprets the built-in validator's OWN (exit code, output)
pair and answers one question: could a board reset possibly fix this? A dead link, yes. A
cabling descriptor that describes other hardware, or a permission error creating a log file,
never — routing those to a reset resets after every job, forever, and converges on nothing. So a
measured-bad verdict is UNHEALTHY; a run that never reached a measurement is SKIPPED; and the two
must never share a signal, because a mesh reported healthy without having been looked at is worse
than not checking it at all. This table only ever applies to the built-in path: an operator's
override command owes the validator's output no resemblance, so it is judged on its exit code
alone (see ``monitor.verify_fabric_health``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from tt_device_mcp.health.monitors.subproc import Terminate, Track, run_probe

# The validator's own throw when generate_link_metrics finds a bad link under --hard-fail. This
# is the ONE place a board reset can act on: every other non-zero exit means the validator never
# finished measuring anything.
_UNHEALTHY_LINKS = re.compile(r"unhealthy ethernet connections")

# A wedged eth core stalls the validator's OWN device init before it can reach a measurement, so
# without this it would fall to SKIPPED — but the firmware's own message says the core is
# resettable, and the check that would trigger recovery is exactly the one the wedge blocks.
_WEDGED_ETH_INIT = re.compile(r"waiting for active ethernet core|Try resetting the board")

# The validator's own per-iteration watchdog: traffic went out and no link ever reported back.
# A stalled traffic pass IS a measurement — the validator itself calls this an unhealthy cluster
# — and it is the dominant reason a host learns nothing about its fabric.
_WATCHDOG_TIMEOUT = re.compile(r"Workload execution timed out after \d+ seconds")

# The first line naming why the validator never reached a verdict, for a SKIPPED detail that
# says more than a bare exit code.
_SKIP_REASON = re.compile(r"what\(\):.*|filesystem error:.*|terminate called.*")


def classify(rc: Optional[int], output: str) -> tuple[Optional[bool], str]:
    """Turn one (exit code, captured output) pair into ``(ok, detail)``: ``True`` healthy,
    ``False`` unhealthy (a reset can act on this), ``None`` skipped (nothing was measured).

    ``rc is None`` means the broker's own timeout fired, not the validator — a run that never
    gets to finish printing its verdict is exactly the wedge this check exists to catch, so it
    is UNHEALTHY, never a skip; there is no bash-side equivalent of this case, since the
    wrapper script always runs to some exit code and it is the broker's ``asyncio.wait_for``
    that can cut it off first.
    """
    if rc == 0:
        return True, "fabric healthy"
    if rc is None:
        return False, "fabric check timed out before reaching a verdict"
    if _UNHEALTHY_LINKS.search(output):
        return False, "the validator measured bad links"
    if _WEDGED_ETH_INIT.search(output):
        return False, (
            "a wedged ethernet core stalled the validator's device init before any "
            "link was measured (resettable — the firmware says so)"
        )
    if _WATCHDOG_TIMEOUT.search(output):
        return False, "traffic stalled until the validator's own watchdog fired; no link reported back"
    reason = _SKIP_REASON.search(output)
    detail = f"validator did not complete a measurement (rc={rc})"
    if reason:
        detail += f": {reason.group(0)}"
    return None, detail


def build_command() -> Optional[tuple[list[str], dict[str, str]]]:
    """The argv + env to run the fabric check with, or ``None`` when nothing is
    configured/available to run at all.

    ``TT_DEVICE_MCP_FABRIC_CHECK_CMD`` set names an operator's own wrapper, run through
    ``/bin/bash -c`` exactly as before — unchanged semantics for anyone pointed at
    ``deploy/tt-device-fabric-check.sh`` or an equivalent.

    Unset, this resolves the pinned validator under ``TTDEV_VALIDATOR_ROOT`` (default
    ``/opt/tt-device-broker/validator``) the same way that script did: the binary at
    ``current/build/tools/scaleout/run_cluster_validation``, run directly against the checkout
    it was built from (``TT_METAL_RUNTIME_ROOT``/``TT_METAL_HOME``) with its own dedicated
    firmware/kernel cache, and an OPTIONAL cabling descriptor — optional because defaulting it
    to any one topology makes the check work on only that topology, where every host gets a
    traffic-only check without it. A binary that has not been published yet is exactly as
    unavailable as no override at all, so both collapse to the same ``None``.
    """
    override = os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()
    if override:
        return ["/bin/bash", "-c", override], {**os.environ, "PYTHONUNBUFFERED": "1"}

    vroot = os.environ.get("TTDEV_VALIDATOR_ROOT", "/opt/tt-device-broker/validator").strip()
    current = f"{vroot}/current"
    binpath = os.environ.get("TTDEV_FABRIC_BIN", "").strip() or f"{current}/build/tools/scaleout/run_cluster_validation"
    if not os.access(binpath, os.X_OK):
        return None
    runtime_root = os.environ.get("TTDEV_FABRIC_RUNTIME_ROOT", "").strip() or current
    if not os.path.isdir(runtime_root):
        return None

    desc = os.environ.get("TTDEV_FABRIC_DESCRIPTOR", "").strip()
    desc_argv: list[str] = []
    if desc:
        if not os.path.isfile(desc):
            return None
        desc_argv = ["--cabling-descriptor-path", desc]

    # Dedicated so the (root-run) check never depends on $HOME and never pollutes a
    # user's own firmware/kernel cache.
    cache = os.environ.get("TTDEV_FABRIC_CACHE", "").strip() or "/var/cache/tt-device-broker/fabric-tt-metal-cache"
    try:
        Path(cache).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    out = os.environ.get("TTDEV_FABRIC_OUTPUT", "").strip() or f"{cache}/cluster_validation_logs"
    try:
        Path(out).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    iters = os.environ.get("TTDEV_FABRIC_ITERS", "").strip() or "1"

    argv = [binpath, *desc_argv, "--output-path", out, "--hard-fail", "--send-traffic", "--num-iterations", iters]
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "TT_METAL_CACHE": cache,
        "TT_METAL_RUNTIME_ROOT": runtime_root,
        "TT_METAL_HOME": runtime_root,
    }
    return argv, env


async def check(
    argv: list[str],
    env: dict[str, str],
    *,
    timeout_sec: float,
    track: Track,
    terminate: Terminate,
    cwd: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """Run an already-built fabric command (see ``build_command()``) and return ``(rc, output)``.

    Split from ``classify()`` on purpose: that decision table means something only when applied
    to the built-in validator's OWN output signatures. An operator's ``TT_DEVICE_MCP_FABRIC_CHECK_CMD``
    wrapper has already done its own interpretation and reports it purely through its exit code
    (see ``monitor.verify_fabric_health``), so its stdout must never be run back through this
    table — it owes the validator's output no resemblance.

    Split from ``build_command()`` so a caller that already knows nothing is configured never
    spawns a process just to learn that again.

    ``cwd`` is the caller's to set, not derived from ``env`` here: ``run_cluster_validation``
    resolves its kernels from its own runtime root and needs to run from that directory, but an
    operator's override command gets none — it runs from wherever the broker itself runs, exactly
    as before this module existed. Deriving ``cwd`` from an ambient ``TT_METAL_RUNTIME_ROOT`` in
    ``env`` would silently relocate an override's cwd too, since ``env`` for that path is just
    ``os.environ`` and the broker's own process may already have that variable set.
    """
    rc, raw = await run_probe(
        argv, timeout_sec=timeout_sec, track=track, stream=True, env=env, cwd=cwd, terminate=terminate
    )
    return rc, raw.decode("utf-8", "replace").strip()
