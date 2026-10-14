# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A job's stdout and stderr, spooled to files that outlive the broker (spec 01 I17).

A job used to write into pipes the broker read. A job outlives a broker restart by design (it
runs in its own systemd scope and the next broker re-adopts it), but a pipe's read end does not:
it dies with the broker that held it. The job's next write then hit EPIPE, and a Python job that
was still printing failed at exit (status 120, a failed final stdout flush). Re-adoption kept the
process and lost the job.

So the job writes into files instead, opened by the broker and handed over as fds, and the broker
tails them. Nothing the job writes depends on a reader being alive. The broker records how far it
has copied into the job log (a small sidecar), so the next broker resumes the copy at that byte
and the log carries every line once. It also punches out what it has read, so the spool stays near
empty while a broker runs and only grows while none does.

The spool lives outside the unit's RuntimeDirectory: systemd removes that directory on every stop,
restart included, which would unlink the very files a re-adopted job still writes into.
"""

import asyncio
import ctypes
import ctypes.util
import os
import stat
from pathlib import Path
from typing import Callable, Optional

JOB_SPOOL_DIR_DEFAULT = "/run/tt-device-broker-spool"
SPOOL_POLL_SEC = 0.1
# A line this long without a newline is handed on as it stands: the reader's memory is bounded.
SPOOL_MAX_LINE_BYTES = 1 << 20
# Punch the consumed prefix once this much more has been read: one syscall per MiB, not per line.
SPOOL_PUNCH_MIN_BYTES = 1 << 20
_PUNCH_ALIGN = 4096
_STREAMS = ("stdout", "stderr")
_FALLOC_FL_KEEP_SIZE = 0x01
_FALLOC_FL_PUNCH_HOLE = 0x02


def job_spool_dir() -> Path:
    """Where job output is spooled. Read per call, like job_exit_dir(): a per-user daemon (and a
    test) redirects it through the environment."""
    return Path(os.environ.get("TT_DEVICE_MCP_JOB_SPOOL_DIR", "").strip() or JOB_SPOOL_DIR_DEFAULT)


def spool_paths(job_id: str) -> dict:
    d = job_spool_dir()
    return {
        "stdout": d / f"{job_id}.stdout",
        "stderr": d / f"{job_id}.stderr",
        "offsets": d / f"{job_id}.offsets",
    }


def _private_dir(d: Path) -> bool:
    """The spool dir is ours alone: a directory, owned by us, writable by nobody else. Job output
    can carry secrets, and a dir another user could write is one they could plant links in."""
    try:
        st = os.lstat(d)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not (st.st_mode & 0o077)


def open_job_spool(job_id: str) -> Optional[dict]:
    """Create the job's spool files; return {"stdout": fd, "stderr": fd} for the child to write.

    None when the spool cannot be had (no such dir and no right to make it, a dir that is not
    private, a full filesystem): the caller then falls back to pipes, as before. A job is never
    refused for want of a spool.
    """
    d = job_spool_dir()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return None
    if not _private_dir(d):
        return None
    paths = spool_paths(job_id)
    fds: dict = {}
    try:
        try:
            paths["offsets"].unlink()
        except FileNotFoundError:
            pass
        for name in _STREAMS:
            fds[name] = os.open(
                paths[name],
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        return fds
    except OSError:
        for fd in fds.values():
            os.close(fd)
        return None


def read_offsets(job_id: str) -> dict:
    """How far the broker that spawned the job had copied each stream into the log. Missing or
    unreadable reads as 0: a line copied twice is better than one lost."""
    try:
        parts = spool_paths(job_id)["offsets"].read_text().split()
        return {name: max(0, int(parts[i])) for i, name in enumerate(_STREAMS)}
    except (OSError, ValueError, IndexError):
        return {name: 0 for name in _STREAMS}


class SpoolOffsets:
    """The sidecar recording how much of each stream is in the job log. One pwrite per update."""

    def __init__(self, job_id: str, start: Optional[dict] = None):
        self.values = dict(start or {name: 0 for name in _STREAMS})
        self.fd: Optional[int] = None
        try:
            self.fd = os.open(
                spool_paths(job_id)["offsets"],
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError:
            self.fd = None

    def set(self, name: str, value: int) -> None:
        self.values[name] = value
        if self.fd is None:
            return
        try:
            os.pwrite(self.fd, f"{self.values['stdout']:020d} {self.values['stderr']:020d}\n".encode(), 0)
        except OSError:
            pass

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


_libc_fallocate = None


def _punch(fd: int, length: int) -> bool:
    """Free [0, length) of a spool file in place. The writer appends (O_APPEND), so the file's
    size and every later offset stay as they were; only the blocks already read go."""
    global _libc_fallocate
    try:
        if _libc_fallocate is None:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            _libc_fallocate = libc.fallocate
            _libc_fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        return _libc_fallocate(fd, _FALLOC_FL_PUNCH_HOLE | _FALLOC_FL_KEEP_SIZE, 0, length) == 0
    except (OSError, AttributeError):
        return False


class SpoolTail:
    """Reads one spool file a line at a time while the job appends to it.

    ``readline`` has a StreamReader's contract: a line with its newline, a final partial line, then
    b"" for EOF. EOF is the job being done (``done()``) with nothing left to read, not the last
    writer closing the file, which a file never signals. ``exited``, when given, completes as the
    job exits and ends the poll wait at once, so a short job is not held up to a poll interval.
    """

    def __init__(self, path: Path, done: Callable[[], bool], start: int = 0, exited: Optional[asyncio.Future] = None):
        # Read-write, never written: fallocate() refuses to punch through a read-only fd (EBADF).
        self.can_punch = True
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except PermissionError:
            self.fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.can_punch = False
        os.lseek(self.fd, start, os.SEEK_SET)
        self.pos = start
        self.buf = b""
        self.done = done
        self.exited = exited
        self.punched = 0

    @property
    def emitted(self) -> int:
        """Bytes of the file handed out as lines so far: where a successor resumes."""
        return self.pos - len(self.buf)

    def _fill(self) -> bool:
        chunk = os.read(self.fd, 65536)
        if not chunk:
            return False
        self.buf += chunk
        self.pos += len(chunk)
        return True

    def _take(self, n: int) -> bytes:
        line, self.buf = self.buf[:n], self.buf[n:]
        return line

    async def readline(self) -> bytes:
        while True:
            i = self.buf.find(b"\n", 0, SPOOL_MAX_LINE_BYTES)
            if i >= 0:
                return self._take(i + 1)
            if len(self.buf) >= SPOOL_MAX_LINE_BYTES:
                return self._take(SPOOL_MAX_LINE_BYTES)
            if self._fill():
                continue
            if self.done():
                # A write can land between the empty read above and the job's exit: read again.
                if self._fill():
                    continue
                return self._take(len(self.buf))
            if self.exited is not None and not self.exited.done():
                # asyncio.wait never cancels what it waits on: the future stays shared.
                await asyncio.wait({self.exited}, timeout=SPOOL_POLL_SEC)
            else:
                await asyncio.sleep(SPOOL_POLL_SEC)

    def release(self) -> None:
        """Punch out what has been handed on, once enough has gathered to be worth a syscall."""
        upto = self.emitted - self.emitted % _PUNCH_ALIGN
        if not self.can_punch or upto - self.punched < SPOOL_PUNCH_MIN_BYTES:
            return
        if _punch(self.fd, upto):
            self.punched = upto
        else:
            self.can_punch = False

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def remove_job_spool(job_id: str) -> None:
    for p in spool_paths(job_id).values():
        try:
            p.unlink()
        except OSError:
            pass


def sweep_job_spools(keep: Callable[[str], bool]) -> list:
    """Remove the spools of every job ``keep`` does not claim. Returns the job ids removed."""
    d = job_spool_dir()
    if not _private_dir(d):
        return []
    removed = set()
    try:
        entries = list(d.iterdir())
    except OSError:
        return []
    for p in entries:
        job_id = p.name.split(".", 1)[0]
        if keep(job_id):
            continue
        try:
            p.unlink()
            removed.add(job_id)
        except OSError:
            pass
    return sorted(removed)
