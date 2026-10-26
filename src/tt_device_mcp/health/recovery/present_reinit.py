"""Spec 04 I104: POST_RESET and board re-init on the chips present, after tt-smi gave up at its driver wait.

tt-smi's Galaxy mesh reset waits for every chip of the mesh to reappear before it issues POST_RESET and
re-inits the boards. When one chip never comes back that wait gives up, and the chips that did come back
are left without POST_RESET and re-init. tt-smi has no command for those two steps alone, so this runs
the same calls it makes, on the chips that are present: the per-device POST_RESET ioctl, then the local
UMD topology discovery that re-inits the boards.

Run as ``python -m tt_device_mcp.health.recovery.present_reinit`` through the broker's reset path (its
quiesce, holder scan and scope). Exit 0 when every present chip took POST_RESET and the re-init ran;
1 otherwise. It never judges the device: the full check with fabric after it does.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

DEV_DIR = "/dev/tenstorrent"


def present_ids(dev_dir: str = DEV_DIR) -> list[int]:
    """The chip ids that have a device node now, ascending."""
    try:
        names = os.listdir(dev_dir)
    except OSError:
        return []
    return sorted(int(n) for n in names if n.isdigit())


def _tt_smi_ioctl() -> tuple[Callable[[int, int], bool], int]:
    """tt-smi's own reset ioctl and its POST_RESET flag."""
    try:
        from tt_smi.utils import IoctlResetFlags, reset_device_ioctl
    except ImportError:  # older tt-smi kept them in reset.py
        from tt_smi.reset import IoctlResetFlags, reset_device_ioctl  # type: ignore[no-redef]
    return reset_device_ioctl, IoctlResetFlags.POST_RESET


def _umd_reinit() -> int:
    """Re-init the boards the way tt-smi does after a mesh reset: local chips only, no wait on link
    training. Returns the number of devices found."""
    from tt_smi.constants import get_default_discovery_options
    from tt_umd import TopologyDiscovery

    options = get_default_discovery_options()
    options.discover_remote_devices = False
    options.wait_on_ethernet_link_training = False
    _, devices = TopologyDiscovery.discover(options=options)
    return len(devices)


def post_reset(ids: list[int], ioctl: Callable[[int, int], bool], flag: int) -> list[int]:
    """POST_RESET on every id at once; returns the ids it failed on."""

    def one(i: int) -> bool:
        try:
            return bool(ioctl(i, flag))
        except OSError:
            return False

    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        ok = list(pool.map(one, ids))
    return [i for i, good in zip(ids, ok) if not good]


def main(
    dev_dir: str = DEV_DIR,
    ioctl: Optional[Callable[[int, int], bool]] = None,
    flag: Optional[int] = None,
    reinit: Optional[Callable[[], int]] = None,
) -> int:
    ids = present_ids(dev_dir)
    if not ids:
        print("Error: no chip present; nothing to re-init.", flush=True)
        return 1
    if ioctl is None or flag is None:
        ioctl, flag = _tt_smi_ioctl()
    print(f"POST_RESET on {len(ids)} present chip(s): {' '.join(map(str, ids))}", flush=True)
    failed = post_reset(ids, ioctl, flag)
    for i in failed:
        print(f"Error: POST_RESET failed for device {dev_dir}/{i}.", flush=True)
    if failed:
        return 1
    try:
        n = (reinit or _umd_reinit)()
    except Exception as exc:  # noqa: BLE001 - any re-init failure is this step's failure
        print(f"Error: board re-init failed: {exc!r}", flush=True)
        return 1
    print(f"Re-initialized {n} boards", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
