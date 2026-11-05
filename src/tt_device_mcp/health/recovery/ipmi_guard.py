# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I91: a broker mesh reset never sends the BMC's legacy retimer reset.

tt-smi's Galaxy tray reset re-powers the trays with the BMC's UBB reset (``ipmitool raw``, OEM
netfn 0x30, UBB reset cmd 0x8B, op_mode 0x3) and, on ANY failure of that call, sends it again with
op_mode ``0x0``. op_mode 0x0 also asserts the PCIe retimer reset, which can take every device
behind the retimers off the bus, NVMe drives included. tt-smi has no switch for the fallback, so
every mesh reset the broker launches runs in a private mount namespace where ``ipmitool`` is this
guard.

Stdlib only, because this one file plays three roles:

- imported by the broker: :func:`refusal`, :func:`is_mesh_reset`, :func:`override_unclassified`,
  :func:`guarded_argv`, :func:`bmc_preflight`, :func:`scan_reset_output`;
- ``python -I ipmi_guard.py --wrap -- <argv>``, inside ``unshare --mount``: mount a copy of this file
  over every ipmitool sudo can resolve, check the mounts, exec ``<argv>``;
- that copy, run as ``ipmitool``: refuse, or exec the real binary;
- ``python -I ipmi_guard.py --wrap-user -- <argv>`` for a non-root server, which cannot make a mount
  namespace: put a copy named ``sudo`` first on PATH (tt-smi sends its tray reset as
  ``sudo ipmitool raw ...``), exec ``<argv>``; that copy refuses, or runs the real
  ``sudo -n ipmitool`` (spec 04 I109).

Nothing turns the guard off: no environment variable, flag or config is read.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable, Optional

# Private to the reset's mount namespace: a tmpfs mounted here holds the guard and the real binary.
GUARD_DIR = "/run/tt-device-mcp-ipmi-guard"
GUARD_NAME = "ipmitool"
REAL_NAME = "ipmitool.real"
# The guard copy's first line after the shebang; the wrapper reads each mount back for it.
GUARD_TAG = "# tt-device-mcp ipmi guard (spec 04 I91)"
REFUSED_MARKER = "tt-device-mcp ipmi guard: REFUSED"
NOT_ARMED_MARKER = "tt-device-mcp ipmi guard: NOT ARMED"
# util-linux unshare's error when the private mount namespace cannot be made (e.g. not root): the
# wrapper never ran, so the guard is not armed and the reset did not run either.
UNSHARE_FAILED_TEXT = "unshare: unshare failed"
GUARD_REFUSED_RC = 97
# tt-smi prints this before it sends op_mode 0x0 (tt_smi/reset.py run_galaxy_ipmi_reset).
TT_SMI_FALLBACK_TEXT = "falling back to legacy retimer reset"
# tt-smi prints its 30 s settle count, then its wait for every chip, only once the BMC tray reset
# returned without raising (tt_smi/reset.py: run_galaxy_ipmi_reset, timed_wait(30),
# run_ubb_wait_for_driver_load). Any of them is the output's proof that the re-power went out.
TT_SMI_REPOWERED = re.compile(r"Waiting for \d+ seconds: \d+|Waiting for all \d+ chips to show up")
# Ubuntu's default sudo secure_path, in its order, then the broker's own PATH: every directory a
# `sudo ipmitool` or a bare `ipmitool` from tt-smi can resolve.
SECURE_PATH_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin")
MOUNT_TIMEOUT_SEC = 10
# The read-only `mc info` before a mesh reset; also the most it adds to one (spec 04 I91 TTR).
BMC_PREFLIGHT_TIMEOUT_SEC = 10
# Spec 04 I109: the local IPMI nodes ipmitool's default (open) interface uses. A non-root process
# that can open none of them reaches the BMC with `sudo -n ipmitool`, never a prompt or password.
IPMI_DEVICE_NODES = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")
SUDO_RULE = "<user> ALL=(root) NOPASSWD: /usr/bin/ipmitool"
# What sudo -n prints when it will not run the command without a password or a rule.
_SUDO_REFUSED = (
    "a password is required",
    "a terminal is required",
    "is not allowed to execute",
    "is not in the sudoers file",
    "may not run sudo",
)
# The prefix of the non-root wrapper's private PATH dir that holds the `sudo` copy.
USER_SHIM_PREFIX = "tt-device-mcp-ipmi-shim-"
# A guard copy older than this belongs to a mesh reset that has ended (the longest one runs minutes).
USER_SHIM_STALE_SEC = 3600
# BMC v0.05.22 is the first that accepts op_mode 0x3.
MIN_BMC_FIRMWARE = (0, 5)
MIN_BMC_AUX_PATCH = 0x16  # 22: the lower of the byte's decimal (0x16) and BCD (0x22) readings

_NETFN_OEM = 0x30
_CMD_UBB_RESET = 0x8B
# tt-smi's reset flags that reach the IPMI tray reset (argparse also takes unambiguous prefixes).
# tt-smi's Galaxy tray reset flags (-glx_reset*, --galaxy_6u_trays_reset), matched anywhere in any token.
_MESH_FLAG_MARKERS = ("glx_reset", "galaxy_6u_t")
# What a token names when it runs tt-smi itself: the console script, the module, or its .py file.
_TT_SMI_NAMES = ("tt-smi", "tt_smi")


def _ipmi_byte(token: str) -> Optional[int]:
    """The value ipmitool's raw parser (C ``strtol`` with base 0) reads from ``token``, or None."""
    t = token.strip().lower()
    try:
        return int(t, 0)
    except ValueError:
        pass
    if t.isdigit():  # strtol base 0 reads a leading 0 as octal ("010"); Python's int(.., 0) refuses it
        try:
            return int(t, 8)
        except ValueError:
            return None
    return None


def refusal(args: list[str]) -> Optional[str]:
    """Why the guard refuses ``ipmitool <args>``, or None when the real binary may run it.

    Refused: the UBB reset (``raw`` with OEM netfn 0x30, UBB reset cmd 0x8B) whose op_mode byte (the 5th after ``raw``)
    is zero in any format or cannot be read, and ``exec`` / ``shell``, which read commands the guard cannot see."""
    low = [str(a).strip().lower() for a in args]
    for verb in ("exec", "shell"):
        if verb in low:
            return f"ipmitool {verb} could carry a BMC legacy retimer reset the guard cannot see"
    for i, tok in enumerate(low):
        if tok != "raw":
            continue
        rest = low[i + 1 :]
        if len(rest) < 2 or _ipmi_byte(rest[0]) != _NETFN_OEM or _ipmi_byte(rest[1]) != _CMD_UBB_RESET:
            continue
        op = _ipmi_byte(rest[4]) if len(rest) > 4 else None
        if op is None:
            return (
                "BMC UBB reset (netfn 0x30, UBB reset cmd 0x8B) with no readable op_mode byte "
                "(it may be the legacy retimer reset 0x0)"
            )
        if op == 0:
            return (
                "BMC UBB reset (netfn 0x30, UBB reset cmd 0x8B) op_mode 0x0 is the legacy retimer "
                "reset: it asserts the PCIe retimer reset and can drop every device behind it, "
                "NVMe drives included"
            )
    return None


def is_mesh_reset(argv: list[str]) -> bool:
    """Whether ``argv`` is a tt-smi Galaxy tray reset, the only tt-smi path that sends the BMC UBB reset.

    The flag counts wherever it sits, whatever launches it: ``sudo tt-smi -glx_reset``,
    ``env X=1 tt-smi -glx_reset``, ``python3 -m tt_smi -glx_reset``, or a wrapper script given the
    flag. A ``TT_DEVICE_MCP_RESET_ARGS`` override is a full argv, so its first word proves nothing."""
    return any(marker in str(a).lower() for a in argv for marker in _MESH_FLAG_MARKERS)


def _names_tt_smi(token: str) -> bool:
    name = os.path.basename(str(token))
    if name.endswith(".py"):
        name = name[: -len(".py")]
    return name in _TT_SMI_NAMES


def override_unclassified(argv: list[str]) -> str:
    """'' when a ``TT_DEVICE_MCP_RESET_ARGS`` argv is classified, else why the guard cannot classify it.

    Classified: a mesh reset (:func:`is_mesh_reset`, run under the guard after the BMC preflight), or
    a tt-smi reset without the Galaxy tray flag (a word that is tt-smi itself, e.g. ``sudo tt-smi -r``),
    which never sends the BMC UBB reset. Anything else, such as a wrapper script given no Galaxy flag,
    could reach the BMC unseen, so the broker does not run it (fail closed)."""
    if is_mesh_reset(argv) or any(_names_tt_smi(a) for a in argv):
        return ""
    return (
        f"TT_DEVICE_MCP_RESET_ARGS ({' '.join(map(str, argv))!r}) names neither tt-smi nor its Galaxy "
        "tray reset flag, so the ipmitool guard cannot tell whether it reaches the BMC"
    )


def is_root() -> bool:
    return os.geteuid() == 0


def ipmi_node_openable() -> bool:
    """Whether this process may open a local IPMI node (as its effective uid, as ipmitool does)."""
    kwargs = {"effective_ids": True} if os.access in os.supports_effective_ids else {}
    return any(os.access(node, os.R_OK | os.W_OK, **kwargs) for node in IPMI_DEVICE_NODES)


def needs_sudo() -> bool:
    """Spec 04 I109: whether a local ipmitool call from this process must go through ``sudo -n``.
    Root, or a user who can open the IPMI node, calls ipmitool directly."""
    return not is_root() and not ipmi_node_openable()


def _local_interface(args: list[str]) -> bool:
    """Whether ``ipmitool <args>`` uses the local (open) interface, the one that needs the node."""
    args = [str(a) for a in args]
    for i, a in enumerate(args):
        if a == "-I" and i + 1 < len(args):
            return args[i + 1] == "open"
        if a.startswith("-I") and len(a) > 2:
            return a[2:] == "open"
    return True


def bmc_argv(argv: list[str]) -> list[str]:
    """Spec 04 I109: ``argv`` as this process must run it to reach the BMC. A local ``ipmitool`` call from
    a non-root process that cannot open the IPMI node becomes ``sudo -n ipmitool ...`` (never a prompt);
    anything else (root, an openable node, a LAN interface, another command) runs as is."""
    argv = [str(a) for a in argv]
    if argv and os.path.basename(argv[0]) == "ipmitool" and _local_interface(argv[1:]) and needs_sudo():
        return ["sudo", "-n", *argv]
    return argv


def sudo_refused(text: str) -> str:
    """'' unless ``text`` is sudo refusing to run ipmitool without a password or a rule; then the plain
    reason, naming the one sudoers rule a non-root server needs (spec 04 I109)."""
    low = (text or "").lower()
    if "sudo" not in low or not any(m in low for m in _SUDO_REFUSED):
        return ""
    return (
        "`sudo -n ipmitool` was refused: this non-root server reaches the BMC only through sudo, and "
        f"needs the sudoers rule `{SUDO_RULE}` (ipmitool only)"
    )


def guarded_argv(argv: list[str]) -> list[str]:
    """``argv`` run inside a private mount namespace where every ipmitool is the guard. A non-root
    process cannot make one; its wrapper puts the guard first on PATH as ``sudo`` instead (I109)."""
    if not is_root():
        return [sys.executable, "-I", os.path.abspath(__file__), "--wrap-user", "--", *argv]
    return [
        "unshare",
        "--mount",
        "--propagation",
        "private",
        "--",
        sys.executable,
        "-I",
        os.path.abspath(__file__),
        "--wrap",
        "--",
        *argv,
    ]


def scan_reset_output(text: str) -> Optional[dict]:
    """What a mesh reset's output says about the legacy retimer reset, or None when nothing.

    ``refused``: the guard stopped a 0x0 request. ``fallback``: tt-smi announced the 0x0 retry.
    ``sent``: the fallback was announced and no refusal follows it, so 0x0 may have reached the
    BMC. ``not_armed``: the wrapper could not arm the guard and did not run the reset."""
    low = (text or "").lower()
    refused = REFUSED_MARKER.lower() in low
    fallback = TT_SMI_FALLBACK_TEXT in low
    not_armed = NOT_ARMED_MARKER.lower() in low or UNSHARE_FAILED_TEXT in low
    if not (refused or fallback or not_armed):
        return None
    return {"refused": refused, "fallback": fallback, "sent": fallback and not refused, "not_armed": not_armed}


def bmc_step_ok(text: str) -> bool:
    """Spec 04 I69 (mesh path): a mesh reset's output shows the BMC tray reset went through on op_mode 0x3.

    True only when tt-smi got past that call (its settle count or its wait for every chip is in the
    output) and nothing in the output says it fell back to 0x0, the guard refused it, or the guard
    was not armed. Only then did the reset reach every tray, and with it every chip the broker removed."""
    return bool(TT_SMI_REPOWERED.search(text or "")) and scan_reset_output(text) is None


def _run_mc_info(timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        bmc_argv(["ipmitool", "mc", "info"]), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


def bmc_preflight(run: Optional[Callable[[float], subprocess.CompletedProcess]] = None) -> str:
    """ "" when the BMC answers ``mc info`` within BMC_PREFLIGHT_TIMEOUT_SEC and its firmware is not
    provably older than 0.05.22; otherwise why the mesh reset must not run. Read-only."""
    run = run or _run_mc_info
    try:
        res = run(BMC_PREFLIGHT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"the BMC did not answer `ipmitool mc info` within {BMC_PREFLIGHT_TIMEOUT_SEC}s"
    except FileNotFoundError:
        return "ipmitool is not installed, so the tray reset cannot reach the BMC"
    except OSError as exc:
        return f"`ipmitool mc info` could not run: {exc}"
    out = f"{res.stdout or ''}{res.stderr or ''}"
    if res.returncode != 0 and sudo_refused(out):
        return sudo_refused(out)
    if res.returncode != 0:
        tail = " ".join(out.split())[-200:]
        return f"the BMC did not answer `ipmitool mc info` (exit {res.returncode}: {tail or 'no output'})"
    old = firmware_too_old(out)
    if old:
        return old
    return ""


def firmware_too_old(mc_info: str) -> str:
    """Why ``mc_info`` shows firmware older than 0.05.22, or "" (also when it cannot be read)."""
    m = re.search(r"^\s*Firmware Revision\s*:\s*(\d+)\.(\d+)", mc_info, re.MULTILINE)
    if not m:
        return ""
    rev = (int(m.group(1)), int(m.group(2)))
    if rev > MIN_BMC_FIRMWARE:
        return ""
    if rev < MIN_BMC_FIRMWARE:
        return f"BMC firmware {rev[0]}.{rev[1]:02d} is older than 0.05.22 and has no op_mode 0x3 tray reset"
    aux = re.search(r"Aux Firmware Rev Info\s*:\s*\n\s*(0x[0-9a-fA-F]+)", mc_info)
    if not aux:
        return ""
    patch = int(aux.group(1), 16)
    if patch < MIN_BMC_AUX_PATCH:
        return (
            f"BMC firmware 0.05 with aux revision 0x{patch:02x} is older than 0.05.22 and has no op_mode 0x3 tray reset"
        )
    return ""


# --- the wrapper (``--wrap``), run as root inside ``unshare --mount --propagation private`` ---


class GuardNotArmed(Exception):
    pass


def ipmitool_targets(dirs: Optional[list[str]] = None) -> list[str]:
    """Every distinct ipmitool binary sudo's secure path or our PATH resolves, secure-path order first."""
    if dirs is None:
        dirs = list(SECURE_PATH_DIRS) + [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    seen: list[str] = []
    for d in dirs:
        p = os.path.join(d, "ipmitool")
        if os.path.isfile(p):
            real = os.path.realpath(p)
            if real not in seen:
                seen.append(real)
    return seen


def _mount(*args: str) -> None:
    try:
        res = subprocess.run(["mount", *args], capture_output=True, text=True, timeout=MOUNT_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardNotArmed(f"mount {' '.join(args)}: {exc}") from exc
    if res.returncode != 0:
        raise GuardNotArmed(f"mount {' '.join(args)}: {(res.stderr or res.stdout).strip()}")


def guard_source() -> str:
    """The guard copy: a shebang for this interpreter, the tag, then this file."""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        body = f.read()
    return f"#!{sys.executable} -I\n{GUARD_TAG}\n{body}"


def arm(
    guard_dir: str = GUARD_DIR, targets: Optional[list[str]] = None, mount: Callable[..., None] = _mount
) -> list[str]:
    """Mount the guard over every ipmitool target; returns the targets. Raises GuardNotArmed."""
    if targets is None:
        try:
            if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
                raise GuardNotArmed("not in a private mount namespace; refusing to mount over the host's ipmitool")
        except OSError as exc:
            raise GuardNotArmed(f"cannot read the mount namespace: {exc}") from exc
        targets = ipmitool_targets()
    if not targets:
        return []  # no ipmitool: tt-smi's IPMI step cannot send anything
    try:
        os.makedirs(guard_dir, exist_ok=True)
    except OSError as exc:
        raise GuardNotArmed(f"mkdir {guard_dir}: {exc}") from exc
    mount("-t", "tmpfs", "-o", "size=1m,mode=0755", "tt-ipmi-guard", guard_dir)
    guard = os.path.join(guard_dir, GUARD_NAME)
    real = os.path.join(guard_dir, REAL_NAME)
    try:
        with open(guard, "w", encoding="utf-8") as f:
            f.write(guard_source())
        # Owner-only: only this namespace's root tt-smi (or its sudo) runs it.
        os.chmod(guard, 0o700)
        open(real, "w").close()
    except OSError as exc:
        raise GuardNotArmed(f"writing the guard: {exc}") from exc
    mount("--bind", targets[0], real)
    for t in targets:
        mount("--bind", guard, t)
        mount("-o", "remount,bind,ro", t)
    for t in targets:
        try:
            with open(t, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError as exc:
            raise GuardNotArmed(f"reading back {t}: {exc}") from exc
        if GUARD_TAG not in head:
            raise GuardNotArmed(f"{t} is not the guard after mounting it")
    return targets


def _wrap_main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(f"{NOT_ARMED_MARKER}: no command given\n")
        return 1
    try:
        arm()
    except GuardNotArmed as exc:
        sys.stderr.write(f"{NOT_ARMED_MARKER}: {exc}; mesh reset NOT run\n")
        sys.stderr.flush()
        return 1
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.stderr.write(f"tt-device-mcp ipmi guard: cannot exec {argv[0]}: {exc}\n")
        return 127
    return 127  # not reached


def _real_tool(name: str) -> Optional[str]:
    """``name`` on sudo's secure path only: the wrapper's own PATH dir holds the guard copy."""
    for d in SECURE_PATH_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _prune_stale_shims(tmp_root: str, now: float) -> None:
    """Remove this user's guard copies left by earlier mesh resets: the wrapper execs into the reset and
    never comes back to remove its own. Only directories this euid owns with the shim prefix, and only
    once older than :data:`USER_SHIM_STALE_SEC`, so a reset still running keeps its copy."""
    import shutil

    try:
        names = os.listdir(tmp_root)
    except OSError:
        return
    for name in names:
        if not name.startswith(USER_SHIM_PREFIX):
            continue
        path = os.path.join(tmp_root, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not os.path.isdir(path) or os.path.islink(path) or st.st_uid != os.geteuid():
            continue
        if now - st.st_mtime >= USER_SHIM_STALE_SEC:
            shutil.rmtree(path, ignore_errors=True)


def _wrap_user_main(argv: list[str]) -> int:
    """Spec 04 I109: the non-root mesh reset. tt-smi sends its tray reset as ``sudo ipmitool raw ...``; a
    copy of this file named ``sudo``, first on PATH, refuses it or runs ``sudo -n ipmitool``."""
    import tempfile
    import time

    if not argv:
        sys.stderr.write(f"{NOT_ARMED_MARKER}: no command given\n")
        return 1
    _prune_stale_shims(tempfile.gettempdir(), time.time())
    try:
        shim_dir = tempfile.mkdtemp(prefix=USER_SHIM_PREFIX)
        shim = os.path.join(shim_dir, "sudo")
        with open(shim, "w", encoding="utf-8") as f:
            f.write(guard_source())
        os.chmod(shim, 0o700)
        with open(shim, encoding="utf-8") as f:
            if GUARD_TAG not in f.read(4096):
                raise OSError(f"{shim} is not the guard after writing it")
    except OSError as exc:
        sys.stderr.write(f"{NOT_ARMED_MARKER}: {exc}; mesh reset NOT run\n")
        sys.stderr.flush()
        return 1
    env = dict(os.environ, PATH=os.pathsep.join([shim_dir, os.environ.get("PATH", "")]))
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        sys.stderr.write(f"tt-device-mcp ipmi guard: cannot exec {argv[0]}: {exc}\n")
        return 127
    return 127  # not reached


def _sudo_shim_main(args: list[str]) -> int:
    """The non-root guard, run as ``sudo``: only ``sudo [-n] ipmitool <args>`` passes, after :func:`refusal`;
    it runs the real ipmitool directly when the node opens, else through the real ``sudo -n``."""
    rest = args[1:] if args[:1] == ["-n"] else args
    if not rest or os.path.basename(rest[0]) != "ipmitool":
        sys.stderr.write(f"{REFUSED_MARKER}: only `sudo ipmitool` passes the guard. Command: sudo {' '.join(args)}\n")
        sys.stderr.flush()
        return GUARD_REFUSED_RC
    why = refusal(rest[1:])
    if why:
        sys.stderr.write(f"{REFUSED_MARKER}: {why}. Command: ipmitool {' '.join(rest[1:])}\n")
        sys.stderr.flush()
        return GUARD_REFUSED_RC
    ipmitool = _real_tool("ipmitool")
    if ipmitool is None:
        sys.stderr.write("tt-device-mcp ipmi guard: ipmitool is not installed\n")
        return 127
    argv = [ipmitool, *rest[1:]]
    if needs_sudo():
        sudo = _real_tool("sudo")
        if sudo is None:
            sys.stderr.write("tt-device-mcp ipmi guard: sudo is not installed\n")
            return 127
        argv = [sudo, "-n", *argv]
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        sys.stderr.write(f"tt-device-mcp ipmi guard: cannot exec {argv[0]}: {exc}\n")
        return 127
    return 127  # not reached


def _guard_main(args: list[str]) -> int:
    why = refusal(args)
    if why:
        sys.stderr.write(f"{REFUSED_MARKER}: {why}. Command: ipmitool {' '.join(args)}\n")
        sys.stderr.flush()
        return GUARD_REFUSED_RC
    real = os.path.join(GUARD_DIR, REAL_NAME)
    try:
        os.execv(real, ["ipmitool", *args])
    except OSError as exc:
        sys.stderr.write(f"tt-device-mcp ipmi guard: cannot exec the real ipmitool ({real}): {exc}\n")
        return 127
    return 127  # not reached


def main(argv: list[str]) -> int:
    if os.path.basename(sys.argv[0]) == "sudo":
        return _sudo_shim_main(argv)
    if argv[:1] == ["--wrap"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        return _wrap_main(rest)
    if argv[:1] == ["--wrap-user"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        return _wrap_user_main(rest)
    return _guard_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
