# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Job-setup preflight: are the job's tt-metal output dirs writable by the user it runs as?

tt-metal writes its debug and log output under ``<logs dir>/generated/`` (watcher, dprint,
inspector, fabric, profiler, noc_data). The logs dir is ``TT_METAL_LOGS_PATH`` when set, else
the process's working directory, which for a broker job is its workspace. A tt-metal session
run as root there leaves ``generated/`` root-owned, and every later job of the workspace's
owner then dies at device open with ``Permission denied [.../generated/watcher/]``, or worse,
sits until its timeout. This module finds that before the job's process starts.

Pure filesystem reads (``stat``, ``listxattr``, ``access``): no device access, nothing is
created, chowned or removed. The check is made for the job's uid and groups, not the broker's,
so a root broker under privsep still sees what its non-root job will see.
"""

import grp
import os
import pwd
import stat
from pathlib import Path
from typing import Iterable, Mapping, Optional

LOGS_PATH_ENV = "TT_METAL_LOGS_PATH"

# The subdirs of generated/ that tt-metal creates on its own (rtoptions/watcher/dprint/
# inspector/control_plane/profiler/noc_logging). A missing one only needs generated/ writable;
# an existing one left root-owned by an earlier session fails the job just as well.
GENERATED_SUBDIRS = ("watcher", "dprint", "inspector", "fabric", "profiler", "noc_data")

_ACL_XATTR = "system.posix_acl_access"


def tt_metal_logs_dir(workspace: str, job_env: Mapping[str, str], inherited_env: Optional[Mapping[str, str]]) -> Path:
    """The dir tt-metal will put ``generated/`` under for this job.

    ``TT_METAL_LOGS_PATH`` from the job's own env first, then from the env the job inherits
    (``inherited_env``, None when it inherits nothing), else the job's working directory, its
    workspace. A relative value is taken from the workspace, where the job starts.
    """
    raw = job_env.get(LOGS_PATH_ENV) or (inherited_env or {}).get(LOGS_PATH_ENV)
    if not raw:
        return Path(workspace)
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else Path(workspace) / path


def output_dirs(logs_dir: Path) -> list[Path]:
    """``generated/`` and its subdirs under ``logs_dir``, parent first."""
    generated = logs_dir / "generated"
    return [generated] + [generated / sub for sub in GENERATED_SUBDIRS]


def groups_for_uid(uid: int) -> Optional[tuple[int, ...]]:
    """The uid's primary and supplementary gids, or None when it has no passwd entry."""
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return None
    try:
        return tuple(os.getgrouplist(pw.pw_name, pw.pw_gid))
    except OSError:
        return (pw.pw_gid,)


def _has_acl(path: Path) -> bool:
    try:
        return _ACL_XATTR in os.listxattr(path)
    except OSError:
        return False


def _writable_by(path: Path, st: os.stat_result, uid: int, gids: Iterable[int]) -> bool:
    """Whether ``uid`` may create entries in the dir ``path`` (write + search).

    Checked as ourselves, the kernel answers (``access``: ACLs, read-only mounts). Checked for
    another uid, the mode bits do; a dir with an ACL is taken as writable rather than refuse a
    job on a rule we do not evaluate.
    """
    if uid == 0:
        return True
    if uid == os.geteuid():
        return os.access(path, os.W_OK | os.X_OK)
    if st.st_uid == uid:
        need = stat.S_IWUSR | stat.S_IXUSR
    elif st.st_gid in set(gids):
        need = stat.S_IWGRP | stat.S_IXGRP
    else:
        need = stat.S_IWOTH | stat.S_IXOTH
    return (st.st_mode & need) == need or _has_acl(path)


def _describe(path: Path, st: os.stat_result) -> str:
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return f"{path} (owner {owner}:{group}, mode {stat.S_IMODE(st.st_mode):04o})"


def _nearest_existing(path: Path) -> tuple[Path, Optional[os.stat_result]]:
    for candidate in path.parents:
        try:
            return candidate, os.stat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return candidate, None
    return Path("/"), None


def check_dir(path: Path, uid: int, gids: Iterable[int]) -> Optional[str]:
    """Why ``uid`` could not write tt-metal output into ``path``, or None when it can.

    An existing ``path`` must be a dir the uid can write; a missing one must be creatable, so
    its nearest existing ancestor must be a dir the uid can write.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        ancestor, ast = _nearest_existing(path)
        if ast is None:
            return f"{path} is missing and {ancestor} cannot be read"
        if not stat.S_ISDIR(ast.st_mode):
            return f"{path} is missing and cannot be created: {_describe(ancestor, ast)} is not a directory"
        if not _writable_by(ancestor, ast, uid, gids):
            return f"{path} is missing and cannot be created: {_describe(ancestor, ast)} is not writable by uid {uid}"
        return None
    except OSError as e:
        return f"{path} cannot be read ({e.strerror or e})"
    if not stat.S_ISDIR(st.st_mode):
        return f"{_describe(path, st)} is not a directory"
    if not _writable_by(path, st, uid, gids):
        return f"{_describe(path, st)} is not writable by uid {uid}"
    return None


def preflight_output_dirs(
    workspace: str,
    job_env: Mapping[str, str],
    inherited_env: Optional[Mapping[str, str]],
    uid: int,
) -> list[str]:
    """Every reason the job's tt-metal output dirs would refuse the job's uid; empty when fine.

    A missing dir whose ancestor already failed is not reported again: one root-owned
    ``generated/`` is one problem, not seven.
    """
    gids = groups_for_uid(uid)
    if gids is None:
        gids = (os.getegid(),) if uid == os.geteuid() else ()
    problems: list[str] = []
    failed: list[Path] = []
    for path in output_dirs(tt_metal_logs_dir(workspace, job_env, inherited_env)):
        if any(path.is_relative_to(bad) for bad in failed) and not path.exists():
            continue
        problem = check_dir(path, uid, gids)
        if problem:
            problems.append(problem)
            failed.append(path)
    return problems
