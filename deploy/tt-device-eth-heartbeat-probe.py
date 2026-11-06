#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Passive active-eth-core heartbeat probe for the tt-device-broker health gate.

Reads each active-ethernet core's firmware heartbeat over ttexalens and decides whether any
core is FROZEN — its link is up but the heartbeat counter has stopped advancing — without
pushing a single packet across the fabric. That is the whole point: the traffic pass this runs
before maps every chip's BARs and drives packets across an already-frozen fabric, which is what
knocks the frozen chip off the PCIe bus and turns a recoverable freeze into a bus drop. A
register read touches no fabric, so it names the frozen core without converting the freeze into
a drop.

Exit codes ARE the contract, resolved through the wrapper to the broker (verify_eth_heartbeat,
server.py). A Python process that dies on an unhandled exception exits 1, so FROZEN uses a
distinct sentinel that a crash cannot forge:
    0   every measured active-eth core's heartbeat is advancing.
    3   at least one measured core is FROZEN (link up, heartbeat not advancing) -> broker HOLDS.
   77   nothing could be measured (no device, no up-link core, the attach failed, or any
        unexpected error) -> broker SKIPS and falls through to the traffic pass. "Could not
        measure" is never "a core is frozen".
Only a run that completed and actually measured >=1 up-link core returns 3; every other outcome —
attach failure, no measurable core, a read that raised, an operator env typo, OOM — is caught and
returned as 77, and the wrapper folds a residual crash (exit 1) or a timeout SIGKILL to 77 too.
FROZEN is therefore only ever a deliberate, evidenced verdict.

Every run that got past the attach also prints one count line ahead of the verdict line:
    eth-links: measured=<n> down=<n> unreadable=<n>
``measured`` is the number of up-link cores whose heartbeat was read. The broker keeps a high-water
mark of it, so a link that went down since the last read shows up as a drop in the count instead of
as one core fewer silently skipped. ``down`` counts cores whose link is not up and ``unreadable``
counts cores whose read raised or came back off-bus. The line changes no exit code.
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from ttexalens import read_word_from_device
from ttexalens.tt_exalens_init import init_ttexalens

# FROZEN is a sentinel distinct from 1 because a Python process that dies on an unhandled exception
# also exits 1. The wrapper must be able to tell a deliberate frozen verdict from a crash, so it
# maps only this code to frozen and folds bare 1 (and every other non-0/3 code) to CANNOT_CHECK.
EXIT_OK = 0
EXIT_FROZEN = 3
EXIT_CANNOT_CHECK = 77

# A BAR read of a chip that has left the PCIe bus returns all-ones. That is a dropped chip — a
# different fault from a frozen-but-present core — so any register reading this value is treated
# as "not on the bus", not as a heartbeat verdict.
OFF_BUS = 0xFFFFFFFF

# Blackhole port_status encoding (check_eth_status.py, pinned validator). 0xFFFFFFFF and every
# non-Up value fall through this map to a non-"Up" result, so an off-bus or down core is rejected
# by the same gate.
PORT_STATUS_MAP = {0: None, 1: "Up", 2: "Down", 3: "Unused"}


@dataclass
class EthRegs:
    """Arch-specific active-eth register offsets, sourced from check_eth_status.py in the pinned
    validator tree, which is the authority for these addresses."""

    port_status: Optional[int]
    rx_link_up: int
    heartbeat: int


BLACKHOLE = EthRegs(port_status=0x7CC04, rx_link_up=0x7CE04, heartbeat=0x7CC70)
WORMHOLE = EthRegs(port_status=None, rx_link_up=0x1EC0 + 0x20, heartbeat=0x1C)


def _read(loc, addr, context) -> int:
    return read_word_from_device(loc, addr, context=context)


def core_is_measurable(loc, regs: EthRegs, context) -> bool:
    """Whether the core's link is up, so a stalled heartbeat means "frozen" and not "unused". An
    off-bus chip reads 0xFFFFFFFF on these registers too, so this same gate rejects a dropped chip
    — its death is not this probe's verdict to render."""
    if regs.port_status is not None:
        if PORT_STATUS_MAP.get(_read(loc, regs.port_status, context)) != "Up":
            return False
    rx = _read(loc, regs.rx_link_up, context)
    return rx not in (0, OFF_BUS)


def heartbeat_verdict(loc, hb_addr: int, context, window_sec: float, poll_sec: float) -> str:
    """ "advancing" | "frozen" | "offbus".

    Seeds the compare with the FIRST real read — never a fixed 0, the bug that made a counter
    frozen at any nonzero value read as advancing on its first sample — then watches for ANY
    change across a real wall-clock window. A live counter, even a slow one, moves within the
    window; only a truly stopped counter reads identical for the whole window."""
    first = _read(loc, hb_addr, context)
    if first == OFF_BUS:
        return "offbus"
    deadline = time.monotonic() + window_sec
    # At least one compare read must happen before "frozen" — the deadline is checked AFTER the
    # compare so a zero/misconfigured window cannot declare frozen off the seed read alone.
    while True:
        cur = _read(loc, hb_addr, context)
        if cur == OFF_BUS:
            return "offbus"
        if cur != first:
            return "advancing"
        if time.monotonic() >= deadline:
            return "frozen"
        time.sleep(poll_sec)


def main() -> int:
    window = float(os.environ.get("TTDEV_ETH_CHECK_HEARTBEAT_WINDOW_SEC", "0.5"))
    poll = float(os.environ.get("TTDEV_ETH_CHECK_POLL_SEC", "0.01"))

    try:
        context = init_ttexalens()
    except Exception as e:  # a failure to reach the cluster is a skip, not a frozen verdict
        print(f"eth-heartbeat-probe: ttexalens attach failed: {e}")
        return EXIT_CANNOT_CHECK

    devices = list(getattr(context, "devices", {}).values())
    if not devices:
        print("eth-heartbeat-probe: no devices attached")
        return EXIT_CANNOT_CHECK

    measured = 0
    down = 0
    unreadable = 0
    frozen: list[str] = []
    for device in devices:
        if device.is_blackhole():
            regs = BLACKHOLE
        elif device.is_wormhole():
            regs = WORMHOLE
        else:
            print(f"eth-heartbeat-probe: dev {device.id}: unsupported arch, skipping")
            continue
        for loc in device.active_eth_block_locations:
            try:
                if not core_is_measurable(loc, regs, context):
                    down += 1
                    continue
                verdict = heartbeat_verdict(loc, regs.heartbeat, context, window, poll)
            except Exception as e:  # one unreadable core must not abort the sweep or fake a verdict
                print(f"eth-heartbeat-probe: dev {device.id} {loc}: read error, skipping: {e}")
                unreadable += 1
                continue
            if verdict == "offbus":
                unreadable += 1
                continue
            measured += 1
            if verdict == "frozen":
                frozen.append(f"dev{device.id}:{loc}")

    print(f"eth-links: measured={measured} down={down} unreadable={unreadable}")
    if measured == 0:
        print("eth-heartbeat-probe: no active-eth core with its link up was measurable")
        return EXIT_CANNOT_CHECK
    if frozen:
        print(f"No heartbeat detected on {len(frozen)}/{measured} active-eth core(s): {', '.join(frozen)}")
        return EXIT_FROZEN
    print(f"all {measured} active-eth core heartbeat(s) advancing")
    return EXIT_OK


def _run() -> int:
    # Any unhandled failure — an operator env typo parsed by float(), a raise while enumerating a
    # degraded chip's cores, OOM — is "the read did not complete", which is CANNOT_CHECK. Without
    # this, such a failure would exit 1 and be indistinguishable from a frozen verdict.
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - a crash must skip, never fake a frozen verdict
        print(f"eth-heartbeat-probe: crashed, cannot check: {e!r}")
        return EXIT_CANNOT_CHECK


if __name__ == "__main__":
    sys.exit(_run())
