# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""What this process can actually execute — half of the recovery ladder's arming (spec 04 I17).

The other half is the platform. Together they decide which rungs exist on this host.

Every probe is a stable condition — a euid, a directory, a binary on PATH, a device node this
process can open — never a trial run of the tool. A probe that shelled out would aim the reset
machinery at the silicon on the boot path, which is the one moment a broker restarting after a
wedge cannot afford it.

The probes are measured once, by `latch()` at boot, and every later read returns that record. The
boot line an operator reads and the rung a wedge later reaches are then the same measurement; a
live re-probe could disagree with the line hours after it was printed.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

# What sd_booted(3) reads. Not `systemctl` on PATH: a restricted-PATH host still runs systemd, and
# reading that as a container would silently drop the scoped-reset backend and the reboot rung.
SYSTEMD_DIR = "/run/systemd/system"

# Where the kernel's IPMI driver puts the local BMC interface, in the spellings different distros
# and driver versions use. `ipmitool raw` with no `-I lanplus` — the form both IPMI rungs issue —
# reaches the BMC through one of these, so no openable node means no reachable BMC.
IPMI_DEVICE_NODES = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")


# One definition per probe. Both readers — the accessors below and the boot line — go through
# these, so the line an operator reads cannot describe a different host from the one the rungs
# were armed against.
_PROBES = {
    "root": lambda: os.geteuid() == 0,
    "systemd": lambda: os.path.isdir(SYSTEMD_DIR),
    "setpci_bin": lambda: shutil.which("setpci") is not None,
    "ipmitool": lambda: shutil.which("ipmitool") is not None,
    "ipmi_node": lambda: _ipmi_node_openable(),
}

# The measurement, taken once by latch() at boot. None until then, and the probes measure live in
# the meantime so a caller that never booted a broker still gets a truthful answer.
_LATCHED: Optional[dict] = None


def _ipmi_node_openable() -> bool:
    """Whether a local IPMI interface exists that this process may open.

    effective_ids: ipmitool opens the node as the effective uid, and every other probe here reads
    geteuid. os.access defaults to the REAL uid, which answers a different question.
    """
    kwargs = {"effective_ids": True} if os.access in os.supports_effective_ids else {}
    return any(os.access(node, os.R_OK | os.W_OK, **kwargs) for node in IPMI_DEVICE_NODES)


def _measure() -> dict:
    return {name: probe() for name, probe in _PROBES.items()}


def _state() -> dict:
    return _LATCHED if _LATCHED is not None else _measure()


def latch() -> dict:
    """Measure every probe once and hold it for the process. Called from `ServerFsm.boot()`."""
    global _LATCHED
    _LATCHED = _measure()
    return dict(_LATCHED)


def is_root() -> bool:
    """Whether this process runs with full authority over the host's devices and services."""
    return _state()["root"]


def has_systemd() -> bool:
    """Whether systemd is running: a PID-1 reset scope and `systemctl reboot` both need it."""
    return _state()["systemd"]


def can_setpci() -> bool:
    """Whether the per-chip bridge reset can issue its Secondary Bus Reset.

    A permission failure from setpci is indistinguishable at the call site from a bad bridge, and
    that reads as retryable — so the capability is decided here rather than discovered by running
    the write. The SBR targets the parent bridge's config space, which the kernel allows only to
    root, so the binary alone is not the capability.
    """
    state = _state()
    return state["setpci_bin"] and state["root"]


def can_ipmi() -> bool:
    """Whether the BMC rungs can reach the BMC: ipmitool plus an openable local IPMI node.

    A host with the binary and no node cannot cold-cycle at all, and I14 requires a rung that
    cannot fire to read OFF rather than be chosen and then raise.
    """
    state = _state()
    return state["ipmitool"] and state["ipmi_node"]


def snapshot() -> dict:
    """Every raw probe at once, for the one boot line that states them.

    Raw, not folded: reporting `setpci` as False on a host that has setpci but is not root sends
    the reader to install a package they already have. The rung inventory names the derived
    reason; this line states the facts it was derived from.
    """
    return {"euid": os.geteuid(), **_state()}
