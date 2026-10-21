# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Witness how every process of a job's tree exits, from the kernel's own exit records.

A preempted job that ends cleanly on SIGTERM (every process exits through its own code) needs
only the fast between-jobs check; one where any process died from a signal may have left an eth
core or the fabric mid-transaction, so the device is marked dirty and needs the full check,
fabric included (a failing check goes on to the reset ladder). The broker cannot tell these
apart from wait statuses: it waits only on its own child, the job's bash wrapper turns any SIGTERM
into a plain `exit 143`, and grandchildren are reaped by their own parents. The kernel's process
events connector (netlink) reports every fork and exit on the host with the exit status in
waitpid form, so it can.

It needs CAP_NET_ADMIN (the root broker has it). When it cannot subscribe, misses events (receive
buffer overflow), or the tree cannot be read, the verdict is "not proven clean", which the
caller treats as a killed job.
"""

import errno
import os
import socket
import struct
from typing import Dict, Iterable, Optional, Set, Tuple

NETLINK_CONNECTOR = 11
CN_IDX_PROC = 1
CN_VAL_PROC = 1
PROC_CN_MCAST_LISTEN = 1
PROC_CN_MCAST_IGNORE = 2
NLMSG_DONE = 3
PROC_EVENT_FORK = 0x00000001
PROC_EVENT_EXIT = 0x80000000

_NLMSGHDR = struct.Struct("=IHHII")  # len, type, flags, seq, pid
_CN_MSG = struct.Struct("=IIIIHH")  # idx, val, seq, ack, len, flags
_EVENT_HDR = struct.Struct("=IIQ")  # what, cpu, timestamp_ns
_FORK = struct.Struct("=iiii")  # parent_pid, parent_tgid, child_pid, child_tgid
_EXIT = struct.Struct("=iiII")  # process_pid, process_tgid, exit_code, exit_signal
_EVENT_AT = _NLMSGHDR.size + _CN_MSG.size

_RCVBUF = 8 << 20


def parse_events(buf: bytes) -> Iterable[Tuple[str, Tuple[int, ...]]]:
    """Yield ("fork", (parent_tgid, child_tgid)) and ("exit", (pid, tgid, wait_status)) from
    one recv of the connector socket. Anything else is skipped."""
    off = 0
    while off + _NLMSGHDR.size <= len(buf):
        nl_len = _NLMSGHDR.unpack_from(buf, off)[0]
        if nl_len < _NLMSGHDR.size or off + nl_len > len(buf):
            return
        at = off + _EVENT_AT
        if at + _EVENT_HDR.size <= off + nl_len:
            what = _EVENT_HDR.unpack_from(buf, at)[0]
            data = at + _EVENT_HDR.size
            if what == PROC_EVENT_FORK and data + _FORK.size <= off + nl_len:
                _, ptgid, _, ctgid = _FORK.unpack_from(buf, data)
                yield "fork", (ptgid, ctgid)
            elif what == PROC_EVENT_EXIT and data + _EXIT.size <= off + nl_len:
                pid, tgid, code, _ = _EXIT.unpack_from(buf, data)
                yield "exit", (pid, tgid, code)
        off += (nl_len + 3) & ~3


def _tree(pgid: Optional[int], scope: Optional[str]) -> Optional[Set[int]]:
    """Every live process in the job's process group or its systemd scope, or None if /proc
    cannot be read."""
    found: Set[int] = set()
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if pgid and os.getpgid(pid) == pgid:
                found.add(pid)
                continue
        except OSError:
            continue
        if scope:
            try:
                with open(f"/proc/{pid}/cgroup") as f:
                    if any(line.rstrip("\n").endswith(f"/{scope}") for line in f):
                        found.add(pid)
            except OSError:
                pass
    return found


def _send_op(sock: socket.socket, op: int) -> None:
    """Send a PROC_CN_MCAST_* control message on the connector socket."""
    body = _CN_MSG.pack(CN_IDX_PROC, CN_VAL_PROC, 0, 0, 4, 0) + struct.pack("=I", op)
    sock.send(_NLMSGHDR.pack(_NLMSGHDR.size + len(body), NLMSG_DONE, 0, 0, 0) + body)


class ExitWitness:
    """Subscribe, snapshot the tree, then call drain() while it dies and clean() once it is gone.

    Order matters: subscribing before the snapshot means no tree process can exit unseen after
    it, and every fork from a tree process after it adds the child to the tree."""

    def __init__(self, pgid: Optional[int], scope: Optional[str]):
        self.ok = False
        self.why = ""
        self.tree: Set[int] = set()
        self.exits: Dict[int, int] = {}  # tgid -> wait status of its leader's exit
        self.signalled: Set[int] = set()
        self._sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_CONNECTOR)
        except OSError as e:
            self.why = f"no process events connector ({e})"
            return
        try:
            for opt in (getattr(socket, "SO_RCVBUFFORCE", 33), socket.SO_RCVBUF):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, opt, _RCVBUF)
                    break
                except OSError:
                    pass
            sock.bind((0, CN_IDX_PROC))
            _send_op(sock, PROC_CN_MCAST_LISTEN)
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            self.why = f"cannot subscribe to process events ({e})"
            return
        self._sock = sock
        tree = _tree(pgid, scope)
        if not tree:
            self.why = "the job's process tree could not be read"
            self.close()
            return
        self.tree = tree
        self.ok = True

    def drain(self) -> None:
        """Read every queued event. A lost event (buffer overflow) or any other failure voids
        the witness, so the caller treats the job as killed."""
        try:
            self._drain()
        except Exception as e:  # noqa: BLE001 - a broken witness must not stop the kill
            self.ok = False
            self.why = f"process events failed ({e!r})"

    def _drain(self) -> None:
        while self.ok and self._sock is not None:
            try:
                buf = self._sock.recv(65536)
            except BlockingIOError:
                return
            except OSError as e:
                self.ok = False
                self.why = "process events were lost" if e.errno == errno.ENOBUFS else f"process events failed ({e})"
                return
            for kind, ev in parse_events(buf):
                if kind == "fork":
                    if ev[0] in self.tree:
                        self.tree.add(ev[1])
                else:
                    pid, tgid, status = ev
                    if tgid not in self.tree:
                        continue
                    if status & 0x7F:
                        self.signalled.add(tgid)
                    if pid == tgid:
                        self.exits[tgid] = status

    def clean(self) -> bool:
        """True only if every process of the tree was seen to exit, none of them by a signal."""
        self.drain()
        if not self.ok:
            return False
        if self.signalled:
            self.why = f"pid {min(self.signalled)} of the job died from a signal"
            return False
        unseen = self.tree - self.exits.keys()
        if unseen:
            self.why = f"pid {min(unseen)} of the job left no exit record"
            return False
        return True

    def close(self) -> None:
        """Unsubscribe, then close. The kernel counts listeners and only IGNORE lowers the
        count; closing the socket alone leaves the host's connector reporting to nobody."""
        if self._sock is not None:
            try:
                _send_op(self._sock, PROC_CN_MCAST_IGNORE)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
