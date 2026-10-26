# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The job logs on disk as the durable job store (spec 01 I71).

Every job's log (header, output, footer) is written to the log dir as the job runs, and that
dir outlives a broker restart and a host reboot. The in-memory job table does not: a restart
empties it, and a status or log call for a job from before the restart used to answer
"Job not found" while the log sat on disk. These helpers find a job's log by id, read its tail
whatever bytes it ends in, and keep the dir bounded by count and size, oldest first.
"""

import os
import re
import signal
import time
from pathlib import Path
from typing import Iterable, Optional

# A job log is `<YYYY-MM-DD>_<HHMMSS>_<id>.log`; name order is time order. Nothing else in the
# dir (server.log, its rotations, the queued-spec subdir) matches.
JOB_LOG_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}_([0-9A-Za-z-]+)\.log$")
# What a client may pass as a job id: no glob or path characters, so a lookup can only ever
# match that one id's logs.
_JOB_ID_RE = re.compile(r"[0-9A-Za-z-]{1,32}")

# Retention defaults: the newest logs are kept until either bound is reached. Both keep the disk
# lean (a few days of a busy box) and far above what the recent list and id reuse look at.
JOB_LOG_KEEP_DEFAULT = 2000
JOB_LOG_MAX_MB_DEFAULT = 512
# A log written this recently is never pruned, whatever the bounds say: it may belong to a job
# still queued, running, or re-adopted by this broker.
JOB_LOG_PRUNE_MIN_AGE_SEC = 3600


def safe_job_id(job_id) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.fullmatch(job_id))


def find_job_log(log_dir, job_id) -> Optional[Path]:
    """The newest log of ``job_id`` in ``log_dir``, or None. Ids are recycled, so an id can
    have older logs too; the newest is the one the id answers to now."""
    if not log_dir or not safe_job_id(job_id):
        return None
    try:
        names = [e.name for e in os.scandir(log_dir) if e.name.endswith(f"_{job_id}.log")]
    except OSError:
        return None
    names = sorted(n for n in names if (m := JOB_LOG_NAME_RE.match(n)) and m.group(1) == job_id)
    return Path(log_dir) / names[-1] if names else None


def read_log_tail(path, n: int, block: int = 65536) -> str:
    """The last ``n`` lines of ``path``, read from the end in blocks (a job log can be
    gigabytes). Bytes that do not decode (binary output, a multibyte character cut by a
    truncated or unflushed write) come back replaced, never raised; a last line with no newline
    is returned as it stands. "" when the file cannot be read."""
    if n <= 0:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= n:
                step = min(block, end)
                end -= step
                f.seek(end)
                data = f.read(step) + data
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[-n:])


def exit_signal(exit_code) -> Optional[str]:
    """The signal a job's exit code names, or None. A negative code is the kernel's word that the
    process died of that signal; 129-192 is the shell's 128+N for a child that did (e.g. 135, a
    bus error), which a program could also exit with on its own, so it is said as derived."""
    try:
        rc = int(exit_code)
    except (TypeError, ValueError):
        return None
    if rc < 0:
        num, derived = -rc, False
    elif 128 < rc <= 192:
        num, derived = rc - 128, True
    else:
        return None
    try:
        name = signal.Signals(num).name
    except ValueError:
        name = f"signal {num}"
    return f"{name} (from exit code {rc} = 128+{num})" if derived else name


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if v > 0 else default


def job_log_limits() -> tuple[int, int]:
    """(most logs kept, most bytes kept). Read per call, like the spool dir."""
    keep = _env_int("TT_DEVICE_MCP_JOB_LOG_KEEP", JOB_LOG_KEEP_DEFAULT)
    max_mb = _env_int("TT_DEVICE_MCP_JOB_LOG_MAX_MB", JOB_LOG_MAX_MB_DEFAULT)
    return keep, max_mb << 20


def prune_job_logs(
    log_dir,
    keep_ids: Iterable[str] = (),
    *,
    max_count: Optional[int] = None,
    max_bytes: Optional[int] = None,
    min_age_sec: float = JOB_LOG_PRUNE_MIN_AGE_SEC,
    now: Optional[float] = None,
) -> list[Path]:
    """Delete the oldest job logs past ``max_count`` logs or ``max_bytes`` in total, whichever
    is reached first, and return what was deleted. Logs of ``keep_ids`` (jobs this broker still
    tracks) and logs written within ``min_age_sec`` are never deleted and count toward neither
    bound. Best effort: a file that cannot be read or deleted is left as it is."""
    if not log_dir:
        return []
    if max_count is None or max_bytes is None:
        dflt_count, dflt_bytes = job_log_limits()
        max_count = dflt_count if max_count is None else max_count
        max_bytes = dflt_bytes if max_bytes is None else max_bytes
    now = time.time() if now is None else now
    keep_ids = set(keep_ids)
    try:
        entries = [(e.name, e) for e in os.scandir(log_dir) if JOB_LOG_NAME_RE.match(e.name)]
    except OSError:
        return []
    count, total, removed = 0, 0, []
    for name, entry in sorted(entries, key=lambda t: t[0], reverse=True):
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if JOB_LOG_NAME_RE.match(name).group(1) in keep_ids or now - st.st_mtime < min_age_sec:
            continue
        if count < max_count and total + st.st_size <= max_bytes:
            count += 1
            total += st.st_size
            continue
        # Past a bound: this one and every older one go. A small older log never slips back
        # under the byte bound a bigger newer one filled, so what is kept is always the newest.
        count, total = max_count, max_bytes
        try:
            os.unlink(entry.path)
            removed.append(Path(entry.path))
        except OSError:
            pass
    return removed
