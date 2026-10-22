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
# is simply not there, mismatches on every run. Not a measurement by itself; the caller reruns
# once without the descriptor (traffic over the discovered links). A link missing with none extra
# is still a failed check (``links_missing``): a reset or power cycle can bring a dead link back.
_TOPOLOGY_MISMATCH = re.compile(r"Connection mismatch detected")
# The wrapper's own line once that traffic-only rerun passed. It carries the mismatch onto an OK
# verdict, so the operator still hears that the cabling is not what the descriptor says.
_TOPOLOGY_MARKER = re.compile(r"TOPOLOGY MISMATCH vs \S+: \d+ missing, \d+ extra port connections[^\n]*")
_MISSING_PORTS = re.compile(r"found (\d+) missing port/cable connections")
_EXTRA_PORTS = re.compile(r"found (\d+) extra port/cable connections")

# The detail prefix of a topology-mismatch skip, so a hold can tell it from any other.
TOPOLOGY_MISMATCH = "topology mismatch"
# The detail prefix of a missing-cable verdict (spec 03 I47): the same prefix a known fabric fault
# (I46) carries, because it is held the same way.
CABLE_FAULT = "known fabric fault"
_MARKER_COUNTS = re.compile(r"(\d+) missing, (\d+) extra port connections")


def topology_counts(output: str) -> tuple[int, int]:
    """``(missing, extra)`` port connections the validator listed against its descriptor."""
    m = _MISSING_PORTS.search(output)
    e = _EXTRA_PORTS.search(output)
    return (int(m.group(1)) if m else 0, int(e.group(1)) if e else 0)


# The validator lists each port connection it found missing or extra under its count line, one
# ``PhysicalPortEndpoint{...}`` pair per line. Counts alone never told an operator which cable to
# look at (spec 03 I71), so the marker carries the names.
_PORT_LIST_HEAD = re.compile(r"found \d+ (missing|extra) port/cable connections")
_PORT_ENDPOINT = re.compile(r"PhysicalPortEndpoint\{([^}]*)\}")
_PORT_FIELD = re.compile(r"(tray_id|asic_location|port_type|port_id)=([^,}\s]+)")
# At most this many names per list go into the marker; the rest are counted.
_MAX_PORT_NAMES = 16


def _port_name(fields: str) -> str:
    f = dict(_PORT_FIELD.findall(fields))
    parts = [f"tray {f.get('tray_id', '?')}"]
    if "asic_location" in f:
        parts.append(f"asic {f['asic_location']}")
    parts.append(f"{f.get('port_type', 'port')} {f.get('port_id', '?')}")
    return " ".join(parts)


def topology_ports(output: str) -> tuple[list[str], list[str]]:
    """``(missing, extra)`` port connection names the validator listed against its descriptor,
    e.g. ``tray 1 QSFP_DD 1 <-> tray 2 QSFP_DD 1``. A list ends at the first line without a port."""
    lists: dict[str, list[str]] = {"missing": [], "extra": []}
    kind = None
    for line in (output or "").splitlines():
        head = _PORT_LIST_HEAD.search(line)
        if head:
            kind = head.group(1)
            continue
        ends = _PORT_ENDPOINT.findall(line)
        if kind is None or not ends:
            kind = None
            continue
        lists[kind].append(" <-> ".join(_port_name(e) for e in ends))
    return lists["missing"], lists["extra"]


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:_MAX_PORT_NAMES])
    more = len(names) - _MAX_PORT_NAMES
    return shown + (f" (+{more} more)" if more > 0 else "")


def topology_marker(desc: str, missing: int, extra: int, ports: tuple[list[str], list[str]] = ([], [])) -> str:
    """The one line the wrapper and the built-in path both log for a mismatch; the built-in path
    appends the port names it read (``ports`` from :func:`topology_ports`)."""
    marker = (
        f"TOPOLOGY MISMATCH vs {desc}: {missing} missing, {extra} extra port connections "
        "(tray ids come from PCI bus numbers)"
    )
    if ports[0]:
        marker += f"; missing ports: {_names(ports[0])}"
    if ports[1]:
        marker += f"; extra ports: {_names(ports[1])}"
    return marker


def marker_ports(marker: str) -> tuple[list[str], list[str]]:
    """The port names a :func:`topology_marker` line carries (none for the wrapper's own line)."""
    out = []
    for kind in ("missing", "extra"):
        m = re.search(rf"; {kind} ports: ([^;]*)", marker or "")
        names = re.sub(r" \(\+\d+ more\)$", "", m.group(1).strip()) if m else ""
        out.append([n.strip() for n in names.split(",") if n.strip()])
    return out[0], out[1]


def with_ports(marker: str, output: str) -> str:
    """``marker`` with the port names ``output`` lists appended, when it carries none yet (the
    wrapper's own line names only counts)."""
    if any(marker_ports(marker)):
        return marker
    missing, extra = topology_ports(output)
    if missing:
        marker += f"; missing ports: {_names(missing)}"
    if extra:
        marker += f"; extra ports: {_names(extra)}"
    return marker


# The detail prefix of a pass whose traffic crossed every discovered link while ports of the
# descriptor were missing (spec 03 I71). A FAILED check, not a lift: the links a job routes over
# the whole mesh are not all there ("Could not find any forwarding direction").
LINKS_MISSING = "fabric links missing"


def names_links_missing(text: str) -> bool:
    """Whether ``text`` carries the I71 verdict."""
    return LINKS_MISSING in (text or "")


def accepted_missing_ports() -> frozenset[str]:
    """Port connections an operator accepts as known dead (``TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS``,
    comma-separated names exactly as the marker prints them). Empty by default: nothing is accepted."""
    raw = os.environ.get("TT_DEVICE_MCP_FABRIC_ACCEPT_MISSING_PORTS", "")
    return frozenset(" ".join(n.split()) for n in raw.split(",") if n.strip())


def links_missing(marker: str, accepted: frozenset[str] = frozenset()) -> str:
    """The FAILED detail for a topology marker whose descriptor ports are missing, else "".

    A moved tray renames its links (as many extra as missing, I42) and stays a lift. More missing
    than extra means links are gone. Only an explicit accept list naming every missing port (all
    names known, none cut off) turns that back into a lift (I71)."""
    m = _MARKER_COUNTS.search(marker or "")
    if not m or int(m.group(1)) <= int(m.group(2)):
        return ""
    missing, extra = int(m.group(1)), int(m.group(2))
    names = marker_ports(marker)[0]
    if accepted and len(names) == missing and set(names) <= accepted:
        return ""
    named = f": {_names(names)}" if names else " (names not listed by the check)"
    return (
        f"{LINKS_MISSING}: {missing} port connection(s) of the cabling descriptor missing, {extra} extra, "
        f"after traffic passed on the discovered links{named}; a job routing over the whole mesh fails"
    )


def marker_counts(text: str) -> tuple[int, int]:
    """(missing, extra) port connections against the cabling descriptor that the topology marker in
    ``text`` names, or (0, 0) when it carries none: the validator prints no link totals, only how far
    the links it found fall short of (or beyond) the cabling."""
    m = _MARKER_COUNTS.search(find_topology_marker(text or "") or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def missing_cables(marker: str) -> int:
    """How many port/cable connections a topology marker names missing with none extra, else 0.

    A moved tray renames its links, so it shows extra connections beside the missing ones; missing
    links with nothing extra are cables that are absent or dead, which no reset can bring back."""
    m = _MARKER_COUNTS.search(marker or "")
    if not m or int(m.group(2)):
        return 0
    return int(m.group(1))


def cable_fault(missing: int, desc: str, why: str) -> str:
    """The detail of a missing-cable verdict; ``why`` is what the traffic-only rerun came to."""
    return (
        f"{CABLE_FAULT}: {missing} port/cable connection(s) of {desc} missing and none extra "
        f"(a cable is absent or dead; no reset can fix it, needs an operator); {why}"
    )


# The wrapper's line when the links passed and the mesh still could not run a program.
DISPATCH_FAILED = "dispatch FAILED on a green fabric"


def names_dispatch_failure(text: str) -> bool:
    """Whether ``text`` carries the wrapper's green-fabric dispatch failure (spec 03 I55)."""
    return DISPATCH_FAILED in (text or "")


# The wrapper's 77 when the links passed and its dispatch probe ran out its cap without a verdict.
DISPATCH_CAP = "dispatch probe hit its"


def names_dispatch_cap(text: str) -> bool:
    """Whether ``text`` carries the wrapper's dispatch-cap 77: the validator had already moved traffic
    across every link, so the links were trained when it gave up (spec 04 I84)."""
    return DISPATCH_CAP in (text or "")


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
    # The wrapper's own final verdict on a mesh whose links passed and whose dispatch did not
    # (spec 03 I55): it names the failure the exit code is about, ahead of any validator line above.
    re.compile(r"dispatch FAILED on a green fabric.*"),
    # Its 77 on the same green fabric, when the dispatch probe ran out its cap (spec 04 I84).
    re.compile(r"dispatch probe hit its .*"),
    re.compile(r"what\(\):.*"),
    re.compile(r"filesystem error:.*"),
    re.compile(r"(?:\|\s*(?:critical|fatal|error)\s*\||\[(?:critical|error)\]|TT_THROW|TT_FATAL).*", re.IGNORECASE),
    re.compile(r"terminate called.*"),
)

# metal's TT_FATAL/TT_THROW header names only the source line and the failed condition
# ("TT_FATAL @ fabric.cpp:173: forwarding_direction.has_value()"); the message itself is on the
# line after a bare "info:" right below it, so the header alone said where it failed and not why.
_ASSERT_HEADER = re.compile(r"TT_(?:FATAL|THROW) @ ")
_ASSERT_INFO = re.compile(r"[^\n]*\n[ \t]*info:[ \t]*\n[ \t]*(\S[^\n]*)")


def first_reason(output: str) -> Optional[str]:
    """The validator's first error line, by :data:`_REASON_PATTERNS` priority, or ``None``.
    A TT_FATAL/TT_THROW header line gets its "info:" message appended.

    Past a ``TOPOLOGY MISMATCH`` marker only the output after it is read: the descriptor run's
    ``Connection mismatch detected`` above it is what the wrapper already downgraded and reran
    without, so it is never why the run that followed failed (spec 03 I55)."""
    marker = _TOPOLOGY_MARKER.search(output)
    if marker:
        output = output[marker.end() :]
    for pat in _REASON_PATTERNS:
        m = pat.search(output)
        if m:
            reason = m.group(0).strip()
            info = _ASSERT_INFO.match(output, m.end()) if _ASSERT_HEADER.search(reason) else None
            if info:
                return f"{reason[:200]} -- {info.group(1).strip()[:200]}"
            return reason[:300]
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
            "descriptor (tray ids come from PCI bus numbers)"
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


# The systemd unit that builds the pinned validator (deploy/tt-device-fabric-validator.service).
VALIDATOR_BUILD_UNIT = "tt-device-fabric-validator.service"
# The shipped wrapper: an override naming it runs the same pinned validator, so it 77s on the same paths.
_SHIPPED_WRAPPER = "tt-device-fabric-check.sh"


def validator_not_built() -> Optional[str]:
    """``validator_missing()`` when the fabric check runs the pinned validator, else ``None`` (spec 03
    I81). That is with no ``TT_DEVICE_MCP_FABRIC_CHECK_CMD``, or one naming the shipped wrapper. An
    operator's own command needs none of these paths, so their absence says nothing about it. Stat
    calls only."""
    override = os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()
    if override and _SHIPPED_WRAPPER not in override:
        return None
    return validator_missing()


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
        argv, timeout_sec=timeout_sec, track=track, stream=True, env=env, cwd=cwd, terminate=terminate, name="fabric"
    )
    return rc, raw.decode("utf-8", "replace").strip()
