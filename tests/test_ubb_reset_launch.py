# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The per-tray BMC reset must launch against the tt-smi API the box actually ships.

The recovery rung wraps the IPMI power pulse in a tt-smi ioctl handshake it imports lazily from
tt_smi.reset. Every other test mocks _fire_ubb_reset wholesale, so nothing exercises that import —
which is how a fire that names a symbol the installed package does not export shipped and threw
ImportError on every real tray-down instead of recovering it.
"""

import subprocess
import sys
import time
import types

import pytest

from tt_device_mcp.health.recovery.stages.ubb_tray import _fire_ubb_reset as _REAL_FIRE_UBB_RESET


def test_fire_ubb_reset_imports_a_symbol_the_installed_tt_smi_defines(monkeypatch):
    """The fire must import a name the installed tt_smi.reset actually exports. The shipped package
    offers the module-level reset_device_ioctl(iid, flag) and no ChipReset class, so importing
    ChipReset makes every fire raise ImportError before it pulses power and the rung silently never
    runs. Fake exactly the installed surface (reset_device_ioctl + IoctlResetFlags, no ChipReset) and
    assert the real fire drives USER_RESET on each chip, pulses the tray once over IPMI, then
    POST_RESET on each chip. Fails on base, which imports the ChipReset the package does not define."""
    calls = []

    class _Flags:
        USER_RESET = "USER_RESET"
        POST_RESET = "POST_RESET"

    fake_reset = types.ModuleType("tt_smi.reset")
    fake_reset.IoctlResetFlags = _Flags
    fake_reset.reset_device_ioctl = lambda iid, flag: calls.append(("ioctl", iid, flag))
    # deliberately no ChipReset — the installed module has none either, so base's import must fail here
    fake_pkg = types.ModuleType("tt_smi")
    fake_pkg.reset = fake_reset
    monkeypatch.setitem(sys.modules, "tt_smi", fake_pkg)
    monkeypatch.setitem(sys.modules, "tt_smi.reset", fake_reset)

    def _fake_run(argv, **_kw):
        calls.append(("ipmitool", tuple(argv)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    _REAL_FIRE_UBB_RESET(0b100, [16, 17, 18])  # UBB2 (bit 2), a few of its chips' interface ids

    assert calls == [
        ("ioctl", 16, "USER_RESET"),
        ("ioctl", 17, "USER_RESET"),
        ("ioctl", 18, "USER_RESET"),
        ("ipmitool", ("ipmitool", "raw", "0x30", "0x8b", "0x04", "0xff", "0x00", "0x0f")),
        ("ioctl", 16, "POST_RESET"),
        ("ioctl", 17, "POST_RESET"),
        ("ioctl", 18, "POST_RESET"),
    ], "the fire must USER_RESET every chip, pulse the tray once over IPMI, then POST_RESET every chip"


def test_fire_ubb_reset_pulses_the_tray_when_a_chip_is_already_off_the_bus(monkeypatch):
    """The chip a tray-down reset exists to recover is already off the bus, so its /dev/tenstorrent
    node is gone and reset_device_ioctl(iid, ...) raises FileNotFoundError. That must NOT abort the
    walk before the IPMI power pulse — the pulse is the only thing that re-powers the tray, so aborting
    on it strands the very drop the rung was armed to clear. Fake the shipped surface, make the downed
    chip's ioctl raise FileNotFoundError both pre- and post-pulse, and assert the tray is still pulsed
    once and the on-bus chips are still quiesced/re-init'd. Fails on base, whose pre-pulse USER_RESET
    loop lets the FileNotFoundError propagate and never reaches subprocess.run."""
    calls = []
    off_bus = 17

    class _Flags:
        USER_RESET = "USER_RESET"
        POST_RESET = "POST_RESET"

    def _ioctl(iid, flag):
        if iid == off_bus:
            raise FileNotFoundError(2, "No such file or directory", f"/dev/tenstorrent/{iid}")
        calls.append(("ioctl", iid, flag))

    fake_reset = types.ModuleType("tt_smi.reset")
    fake_reset.IoctlResetFlags = _Flags
    fake_reset.reset_device_ioctl = _ioctl
    fake_pkg = types.ModuleType("tt_smi")
    fake_pkg.reset = fake_reset
    monkeypatch.setitem(sys.modules, "tt_smi", fake_pkg)
    monkeypatch.setitem(sys.modules, "tt_smi.reset", fake_reset)

    def _fake_run(argv, **_kw):
        calls.append(("ipmitool", tuple(argv)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    _REAL_FIRE_UBB_RESET(0b100, [16, 17, 18])  # chip 17 is off the bus — the drop being recovered

    assert calls == [
        ("ioctl", 16, "USER_RESET"),
        ("ioctl", 18, "USER_RESET"),
        ("ipmitool", ("ipmitool", "raw", "0x30", "0x8b", "0x04", "0xff", "0x00", "0x0f")),
        ("ioctl", 16, "POST_RESET"),
        ("ioctl", 18, "POST_RESET"),
    ], "an off-bus chip is skipped, never aborting the tray power pulse or the on-bus chips' handshake"


def test_fire_ubb_reset_falls_back_to_the_chip_reset_class_on_older_tt_smi(monkeypatch):
    """Older tt-smi exposes the ioctl only as ChipReset().reset_device_ioctl, with no module-level
    reset_device_ioctl. The fire must drive it there too: a tt-smi version pin must never turn the
    lightest recovery rung into a no-op that logs a spurious hardware failure and skips to a heavier
    rung. Fake exactly that surface (ChipReset + IoctlResetFlags, no module-level function) and assert
    the same USER_RESET / IPMI pulse / POST_RESET sequence. Fails against a module-level-only fire,
    which ImportErrors on this surface."""
    calls = []

    class _Flags:
        USER_RESET = "USER_RESET"
        POST_RESET = "POST_RESET"

    class _ChipReset:
        def reset_device_ioctl(self, iid, flag):
            calls.append(("ioctl", iid, flag))
            return True

    fake_reset = types.ModuleType("tt_smi.reset")
    fake_reset.IoctlResetFlags = _Flags
    fake_reset.ChipReset = _ChipReset
    # deliberately no module-level reset_device_ioctl — older tt-smi had none
    fake_pkg = types.ModuleType("tt_smi")
    fake_pkg.reset = fake_reset
    monkeypatch.setitem(sys.modules, "tt_smi", fake_pkg)
    monkeypatch.setitem(sys.modules, "tt_smi.reset", fake_reset)

    def _fake_run(argv, **_kw):
        calls.append(("ipmitool", tuple(argv)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    _REAL_FIRE_UBB_RESET(0b100, [16, 17, 18])

    assert calls == [
        ("ioctl", 16, "USER_RESET"),
        ("ioctl", 17, "USER_RESET"),
        ("ioctl", 18, "USER_RESET"),
        ("ipmitool", ("ipmitool", "raw", "0x30", "0x8b", "0x04", "0xff", "0x00", "0x0f")),
        ("ioctl", 16, "POST_RESET"),
        ("ioctl", 17, "POST_RESET"),
        ("ioctl", 18, "POST_RESET"),
    ], "the fire must USER_RESET every chip, pulse the tray once over IPMI, then POST_RESET every chip"


def test_a_non_zero_bmc_exit_raises_with_its_exit_code(monkeypatch):
    """A BMC command that exits non-zero raises before the settle and the POST_RESET half, and the
    error carries the exit code so the sweep can journal it as ``rc``."""
    import pytest

    from tt_device_mcp.health.recovery.stages.ubb_tray import UbbResetError

    calls = []

    class _Flags:
        USER_RESET = "USER_RESET"
        POST_RESET = "POST_RESET"

    fake_reset = types.ModuleType("tt_smi.reset")
    fake_reset.IoctlResetFlags = _Flags
    fake_reset.reset_device_ioctl = lambda iid, flag: calls.append(("ioctl", iid, flag))
    fake_pkg = types.ModuleType("tt_smi")
    fake_pkg.reset = fake_reset
    monkeypatch.setitem(sys.modules, "tt_smi", fake_pkg)
    monkeypatch.setitem(sys.modules, "tt_smi.reset", fake_reset)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **_kw: types.SimpleNamespace(returncode=1, stdout="", stderr="no BMC\n")
    )
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: calls.append(("sleep",)))

    with pytest.raises(UbbResetError) as err:
        _REAL_FIRE_UBB_RESET(0x08, [24])

    assert err.value.returncode == 1
    assert isinstance(err.value, RuntimeError)
    assert "exited 1: no BMC" in str(err.value)
    assert calls == [("ioctl", 24, "USER_RESET")], "no settle and no POST_RESET after a failed pulse"
