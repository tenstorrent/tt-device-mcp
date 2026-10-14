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

# Links the cabling descriptor expects are still missing after the validator's own five link
# retrains. Reachable only with a descriptor set (no descriptor = no connectivity comparison), and a
# descriptor is set only on a host it describes, so this is a measured fault, not a could-not-check:
# a galaxy reset retrains the links. Left as a skip it held a galaxy fabric-unverified with no
# release path after a reset left links down (jobs then failed "no forwarding direction").
_LINKS_MISSING_AFTER_RETRAIN = re.compile(r"Encountered unrecoverable state")

# The validator's own throw when the links it discovered do not match the cabling descriptor. Its
# tray ids come from PCI bus numbers, so a tray whose uplink lands on another bus, or a cable that
# is simply not there, mismatches on every run: a board reset cannot change either. Never
# measured-bad; the caller reruns once without the descriptor (traffic over the discovered links).
_TOPOLOGY_MISMATCH = re.compile(r"Connection mismatch detected")
# The wrapper's own line once that traffic-only rerun passed. It carries the mismatch onto an OK
# verdict, so the operator still hears that the cabling is not what the descriptor says.
_TOPOLOGY_MARKER = re.compile(r"TOPOLOGY MISMATCH vs \S+: \d+ missing, \d+ extra port connections[^\n]*")
_MISSING_PORTS = re.compile(r"found (\d+) missing port/cable connections")
_EXTRA_PORTS = re.compile(r"found (\d+) extra port/cable connections")

# The detail prefix of a topology-mismatch skip, so a hold can tell it from any other.
TOPOLOGY_MISMATCH = "topology mismatch"


def topology_counts(output: str) -> tuple[int, int]:
    """``(missing, extra)`` port connections the validator listed against its descriptor."""
    m = _MISSING_PORTS.search(output)
    e = _EXTRA_PORTS.search(output)
    return (int(m.group(1)) if m else 0, int(e.group(1)) if e else 0)


def topology_marker(desc: str, missing: int, extra: int) -> str:
    """The one line the wrapper and the built-in path both log for a mismatch."""
    return (
        f"TOPOLOGY MISMATCH vs {desc}: {missing} missing, {extra} extra port connections "
        "(tray ids come from PCI bus numbers; a board reset cannot change this)"
    )


def find_topology_marker(output: str) -> Optional[str]:
    m = _TOPOLOGY_MARKER.search(output or "")
    return m.group(0).strip() if m else None


def is_topology_mismatch(output: str) -> bool:
    """Whether the validator stopped on the descriptor comparison, not on a measurement."""
    return bool(_TOPOLOGY_MISMATCH.search(output or "")) and not find_topology_marker(output)


# A job that fails on the fabric right after a topology-mismatch lift: the links the traffic-only
# pass skipped are exactly the ones a workload that routes across the whole mesh needs.
_JOB_FABRIC_ERRORS = re.compile(
    r"could not find any forwarding direction|fabric router sync: timeout|waiting for active ethernet core",
    re.IGNORECASE,
)


def job_fabric_error(log_tail: str) -> Optional[str]:
    m = _JOB_FABRIC_ERRORS.search(log_tail or "")
    return m.group(0) if m else None


# The lines naming why the validator never reached a verdict, most specific first, for a SKIPPED
# detail that says more than a bare exit code. Order matters: an uncaught exception prints
# "terminate called after throwing an instance of '<type>'" on the line ABOVE "  what():  <cause>",
# so a first-match-in-line-order search kept the exception type and dropped the cause (a missing
# 1G hugepage pool read as a bare UmdException). A logged error/TT_THROW line is next: some aborts
# print their cause only there.
_REASON_PATTERNS = (
    re.compile(r"what\(\):.*"),
    re.compile(r"filesystem error:.*"),
    re.compile(r"(?:\|\s*(?:critical|fatal|error)\s*\||\[(?:critical|error)\]|TT_THROW|TT_FATAL).*", re.IGNORECASE),
    re.compile(r"terminate called.*"),
)


def first_reason(output: str) -> Optional[str]:
    """The validator's first error line, by :data:`_REASON_PATTERNS` priority, or ``None``."""
    for pat in _REASON_PATTERNS:
        m = pat.search(output)
        if m:
            return m.group(0).strip()[:300]
    return None


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
        marker = find_topology_marker(output)
        if marker:
            return True, "fabric healthy on discovered links; " + marker
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
    if is_topology_mismatch(output):
        missing, extra = topology_counts(output)
        return None, (
            f"{TOPOLOGY_MISMATCH}: {missing} missing, {extra} extra port connections vs the cabling "
            "descriptor (tray ids come from PCI bus numbers; a board reset cannot change this)"
        )
    if _LINKS_MISSING_AFTER_RETRAIN.search(output):
        return False, "links the cabling descriptor expects are still missing after the validator's retrains"
    reason = first_reason(output)
    detail = f"validator did not complete a measurement (rc={rc})"
    if reason:
        detail += f": {reason}"
    return None, detail


def _validator_paths() -> tuple[str, str, str]:
    """``(binary, runtime root, descriptor or "")`` of the pinned validator, from the same env the
    wrapper script reads."""
    vroot = os.environ.get("TTDEV_VALIDATOR_ROOT", "/opt/tt-device-broker/validator").strip()
    current = f"{vroot}/current"
    binpath = os.environ.get("TTDEV_FABRIC_BIN", "").strip() or f"{current}/build/tools/scaleout/run_cluster_validation"
    runtime_root = os.environ.get("TTDEV_FABRIC_RUNTIME_ROOT", "").strip() or current
    return binpath, runtime_root, os.environ.get("TTDEV_FABRIC_DESCRIPTOR", "").strip()


def validator_missing() -> Optional[str]:
    """What of the pinned validator is not on disk yet, or ``None`` once all of it is.

    The same three things the wrapper script exits 77 on: the binary, its runtime root and, when one
    is configured, the cabling descriptor. ``install-fabric-validator.sh`` publishes them together by
    flipping the ``current`` link, which on a fresh boot can land minutes after the broker's startup
    pass ran. Stat calls only, so a caller may poll it."""
    binpath, runtime_root, desc = _validator_paths()
    if not os.access(binpath, os.X_OK):
        return f"pinned validator not installed yet ('{binpath}')"
    if not os.path.isdir(runtime_root):
        return f"validator runtime root '{runtime_root}' missing"
    if desc and not os.path.isfile(desc):
        return f"cabling descriptor '{desc}' missing"
    return None


def build_command(descriptor: bool = True) -> Optional[tuple[list[str], dict[str, str]]]:
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

    ``descriptor=False`` drops the cabling descriptor: the traffic-only rerun after a topology
    mismatch.
    """
    override = os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()
    if override:
        return ["/bin/bash", "-c", override], {**os.environ, "PYTHONUNBUFFERED": "1"}

    if validator_missing() is not None:
        return None
    binpath, runtime_root, desc = _validator_paths()
    desc_argv = ["--cabling-descriptor-path", desc] if desc and descriptor else []

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
