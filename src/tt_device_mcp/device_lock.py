# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bare-metal device lock's state (spec 05 I9), shared by the CLI and the broker.

Locked means every device node is mode 0660 and group ``ttdev``: one node left world-rw is a lock
that denies nobody on that chip. Nothing here opens a device node; it stats them, reads the group
database and, for the broker's one opt-in flip, asks udev to re-apply its rules.
"""

import glob
import grp
import json
import os
import pwd
import subprocess
import time
from pathlib import Path
from typing import Optional

LOCK_GROUP = "ttdev"
LOCK_MODE = 0o660
# The tenant/infrastructure boundary (device_holders.MIN_TENANT_UID): a member below it is a
# service account, not a person who could open the node by hand.
MIN_MEMBER_UID = 1000
_DEV_GLOB = "/dev/tenstorrent/*"
# Written once every node has been seen locked since boot (the broker's flip, `lock`, or
# reconcile's check): from then on every job carries the device gid, so reconcile may re-trigger
# udev on drift without cutting off a job that started under its old gid. /run: a reboot clears it.
APPLIED_MARKER = "/run/tt-device-broker/device_lock_applied"
# The drift episode reconcile or the broker recorded; its presence also keeps the event to one per episode.
DRIFT_RECORD = "/run/tt-device-broker/device_lock_drift"
_UDEV_TIMEOUT_SEC = 30


def device_nodes() -> list:
    """The device nodes, and only those. /dev/tenstorrent also holds a `by-id/`
    directory (0755 root:root) — sampling it for permissions reads as 'unlocked' on
    a locked host, so it must never be mistaken for a device."""
    return sorted(p for p in glob.glob(_DEV_GLOB) if os.path.basename(p).isdigit())


def node_drift(nodes: list) -> list:
    """The nodes that are not mode 0660 group ``ttdev``. A node gone between the glob and the
    stat is skipped (a chip leaving the bus is not drift); any other stat error is drift."""
    try:
        want_gid: Optional[int] = grp.getgrnam(LOCK_GROUP).gr_gid
    except KeyError:
        want_gid = None
    bad = []
    for node in nodes:
        try:
            st = os.stat(node)
        except FileNotFoundError:
            continue
        except OSError:
            bad.append(node)
            continue
        if (st.st_mode & 0o777) != LOCK_MODE or st.st_gid != want_gid:
            bad.append(node)
    return bad


def human_members(group: str = LOCK_GROUP) -> list:
    """Members of the lock group with uid >= 1000. Each opens the node without the broker, so
    for them the lock is not in effect. A name with no passwd entry cannot log in: skipped."""
    try:
        names = grp.getgrnam(group).gr_mem
    except KeyError:
        return []
    out = []
    for name in names:
        try:
            if pwd.getpwnam(name).pw_uid >= MIN_MEMBER_UID:
                out.append(name)
        except KeyError:
            continue
    return out


def not_effective_lines(members: list) -> list:
    return [f"lock not effective for {u}" for u in members]


def configured(env=None) -> bool:
    """The broker runs jobs with the device group: the lock is configured on this host."""
    env = os.environ if env is None else env
    return bool(env.get("TT_DEVICE_MCP_DEVICE_GROUP"))


def opted_in(env=None) -> bool:
    """The host opted in (TTDEV_LOCK=1, rendered into the unit): the broker flips the modes."""
    env = os.environ if env is None else env
    return configured(env) and env.get("TT_DEVICE_MCP_DEVICE_LOCK", "").strip() == "1"


def mark_applied() -> None:
    try:
        Path(APPLIED_MARKER).parent.mkdir(parents=True, exist_ok=True)
        Path(APPLIED_MARKER).write_text(f"{int(time.time())}\n")
        Path(DRIFT_RECORD).unlink(missing_ok=True)
    except OSError:
        pass


def clear_markers() -> None:
    for path in (APPLIED_MARKER, DRIFT_RECORD):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def read_drift_record() -> Optional[dict]:
    try:
        rec = json.loads(Path(DRIFT_RECORD).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def record_drift(nodes: list, source: str) -> bool:
    """Write the drift episode for /health and say whether it is new (the caller then journals
    the ``device_lock_drift`` event once). Never raises."""
    if read_drift_record() is not None:
        return False
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "nodes": list(nodes),
    }
    try:
        Path(DRIFT_RECORD).parent.mkdir(parents=True, exist_ok=True)
        Path(DRIFT_RECORD).write_text(json.dumps(rec) + "\n")
    except OSError:
        pass
    return True


def flip(run=subprocess.run) -> list:
    """Ask udev to re-apply its rules to the tenstorrent nodes (the rule installed by the opt-in
    sets 0660 ttdev), wait for it to settle, and return the nodes still not locked. Opens none."""
    for argv in (["udevadm", "trigger", "--subsystem-match=tenstorrent"], ["udevadm", "settle", "--timeout=20"]):
        try:
            run(argv, check=False, timeout=_UDEV_TIMEOUT_SEC, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    return node_drift(device_nodes())


def health_view(env=None) -> Optional[dict]:
    """The /health ``device_lock`` block, or None where the lock is not configured (no change to
    the body of a host that never opted in)."""
    env = os.environ if env is None else env
    if not configured(env):
        return None
    nodes = device_nodes()
    drift = node_drift(nodes)
    view = {
        "opted_in": opted_in(env),
        "locked": bool(nodes) and not drift,
        "nodes": len(nodes),
        "drift": drift,
        "not_effective": not_effective_lines(human_members()),
    }
    rec = read_drift_record()
    if rec is not None:
        view["drift_event"] = rec
    return view
