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
- that copy, run as ``ipmitool``: refuse, or exec the real binary.

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

    Refused: the UBB reset (``raw`` with OEM netfn 0x30, UBB reset cmd 0x8B) whose op_mode byte
    (the 5th after ``raw``) is zero in any format or cannot be read, and ``exec`` / ``shell``,
    which read commands the guard cannot see."""
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


def guarded_argv(argv: list[str]) -> list[str]:
    """``argv`` run inside a private mount namespace where every ipmitool is the guard."""
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
    return subprocess.run(["ipmitool", "mc", "info"], capture_output=True, text=True, timeout=timeout)


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
    if argv[:1] == ["--wrap"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        return _wrap_main(rest)
    return _guard_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
