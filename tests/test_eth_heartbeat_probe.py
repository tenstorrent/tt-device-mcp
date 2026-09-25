# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The decision logic of the passive eth-core heartbeat probe.

The probe reads each active-eth core's firmware heartbeat over ttexalens and decides
FROZEN vs advancing without pushing traffic — the traffic pass it replaces is what knocks
a frozen chip off the bus. The device read is the one part that needs a real cluster; the
verdict logic is pure and is where the failure that motivated this probe lives: the detector
it replaced seeded its compare at 0, so a counter frozen at any nonzero value read as
advancing. Every branch below is exercised with a scripted reader — no device, no ttexalens.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PROBE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-eth-heartbeat-probe.py"


def _load_probe():
    """Load the probe module. ttexalens lives only in a user dev tree, not the test env, and
    the probe imports it at module scope; a stub lets the pure logic import off-box. A real
    ttexalens (on a dev box) is used as-is — every test overrides the reader regardless."""
    try:
        import ttexalens  # noqa: F401
    except Exception:
        tt = types.ModuleType("ttexalens")

        def _unpatched(*_a, **_k):
            raise AssertionError("read_word_from_device must be monkeypatched per test")

        tt.read_word_from_device = _unpatched
        init_mod = types.ModuleType("ttexalens.tt_exalens_init")
        init_mod.init_ttexalens = lambda *_a, **_k: None
        tt.tt_exalens_init = init_mod
        sys.modules["ttexalens"] = tt
        sys.modules["ttexalens.tt_exalens_init"] = init_mod

    spec = importlib.util.spec_from_file_location("eth_heartbeat_probe", _PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()

# A generic core location and context; the scripted reader ignores both.
LOC = object()
CTX = object()


class ScriptedReader:
    """Stands in for read_word_from_device, keyed by register address. A value may be a constant,
    a list consumed one-per-read (the last entry repeats once exhausted, modelling a settled
    register), or a callable(read_index) for a monotonically moving counter."""

    def __init__(self, values):
        self._values = {addr: (list(v) if isinstance(v, list) else v) for addr, v in values.items()}
        self.reads = 0

    def __call__(self, loc, addr, context=None):
        self.reads += 1
        v = self._values[addr]
        if callable(v):
            return v(self.reads)
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v


def _install(monkeypatch, values):
    reader = ScriptedReader(values)
    monkeypatch.setattr(probe, "read_word_from_device", reader)
    return reader


# --- heartbeat_verdict: the frozen/advancing decision ------------------------


def test_frozen_at_nonzero_reads_frozen(monkeypatch):
    """The whole reason the probe exists: a counter stopped at a nonzero value is FROZEN. The
    replaced detector seeded its compare at 0 and called this advancing on the first read."""
    _install(monkeypatch, {probe.BLACKHOLE.heartbeat: 0x1234})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.02, 0.001) == "frozen"


def test_frozen_at_zero_reads_frozen(monkeypatch):
    """0 is a real heartbeat value, not an unseeded sentinel — a counter stuck at 0 is frozen."""
    _install(monkeypatch, {probe.BLACKHOLE.heartbeat: 0})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.02, 0.001) == "frozen"


def test_advancing_counter_reads_advancing(monkeypatch):
    _install(monkeypatch, {probe.BLACKHOLE.heartbeat: lambda n: 100 + n})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.02, 0.001) == "advancing"


def test_offbus_on_first_read(monkeypatch):
    """All-ones is a dropped chip, a different fault the probe must not render as its verdict."""
    _install(monkeypatch, {probe.BLACKHOLE.heartbeat: probe.OFF_BUS})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.02, 0.001) == "offbus"


def test_offbus_mid_window(monkeypatch):
    """A chip that leaves the bus mid-probe is off-bus, not frozen."""
    _install(monkeypatch, {probe.BLACKHOLE.heartbeat: [0x55, 0x55, probe.OFF_BUS]})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.5, 0.001) == "offbus"


def test_zero_window_still_compares_before_deciding(monkeypatch):
    """A zero/misconfigured window must not declare frozen off the seed read alone: at least one
    compare read happens, so a live counter is still caught as advancing."""
    reader = _install(monkeypatch, {probe.BLACKHOLE.heartbeat: [100, 101]})
    assert probe.heartbeat_verdict(LOC, probe.BLACKHOLE.heartbeat, CTX, 0.0, 0.0) == "advancing"
    assert reader.reads >= 2


# --- core_is_measurable: the link-up gate ------------------------------------


def test_blackhole_link_up_is_measurable(monkeypatch):
    _install(monkeypatch, {probe.BLACKHOLE.port_status: 1, probe.BLACKHOLE.rx_link_up: 0x1})
    assert probe.core_is_measurable(LOC, probe.BLACKHOLE, CTX) is True


def test_blackhole_port_down_not_measurable(monkeypatch):
    """Port not Up -> a stalled heartbeat means 'unused', not 'frozen'."""
    _install(monkeypatch, {probe.BLACKHOLE.port_status: 2, probe.BLACKHOLE.rx_link_up: 0x1})
    assert probe.core_is_measurable(LOC, probe.BLACKHOLE, CTX) is False


def test_blackhole_rx_link_down_not_measurable(monkeypatch):
    _install(monkeypatch, {probe.BLACKHOLE.port_status: 1, probe.BLACKHOLE.rx_link_up: 0})
    assert probe.core_is_measurable(LOC, probe.BLACKHOLE, CTX) is False


def test_offbus_core_not_measurable(monkeypatch):
    """An off-bus core reads all-ones on these registers too; the same gate rejects a dropped chip
    so its death is not this probe's verdict to render."""
    _install(monkeypatch, {probe.BLACKHOLE.port_status: probe.OFF_BUS, probe.BLACKHOLE.rx_link_up: probe.OFF_BUS})
    assert probe.core_is_measurable(LOC, probe.BLACKHOLE, CTX) is False


def test_wormhole_no_port_status_uses_rx_link(monkeypatch):
    """Wormhole has no port_status register, so measurability rests on rx_link_up alone."""
    _install(monkeypatch, {probe.WORMHOLE.rx_link_up: 0x1})
    assert probe.core_is_measurable(LOC, probe.WORMHOLE, CTX) is True
    _install(monkeypatch, {probe.WORMHOLE.rx_link_up: 0})
    assert probe.core_is_measurable(LOC, probe.WORMHOLE, CTX) is False


# --- main()/_run(): the exit-code contract -----------------------------------
# The exit code IS the contract the wrapper and broker act on (0=advancing, 3=frozen,
# 77=cannot-check). FROZEN is the only outcome that makes the broker HOLD, so every path
# that is not a completed measurement of >=1 up-link core must resolve to 77 — a crash,
# an attach failure, a per-core read error, an off-bus chip. These tests pin that a
# non-measurement can never surface as 3.


class FakeDevice:
    def __init__(self, dev_id, arch, locs):
        self.id = dev_id
        self._arch = arch
        self.active_eth_block_locations = locs

    def is_blackhole(self):
        return self._arch == "blackhole"

    def is_wormhole(self):
        return self._arch == "wormhole"


class FakeContext:
    def __init__(self, devices):
        self.devices = {d.id: d for d in devices}


def _fast_window(monkeypatch):
    """Shrink the frozen-detection window so a frozen verdict resolves in ms, not the 0.5s default."""
    monkeypatch.setenv("TTDEV_ETH_CHECK_HEARTBEAT_WINDOW_SEC", "0.02")
    monkeypatch.setenv("TTDEV_ETH_CHECK_POLL_SEC", "0.001")


def _attach(monkeypatch, devices):
    monkeypatch.setattr(probe, "init_ttexalens", lambda *_a, **_k: FakeContext(devices))


def test_main_attach_failure_is_cannot_check(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("no cluster")

    monkeypatch.setattr(probe, "init_ttexalens", _raise)
    assert probe.main() == probe.EXIT_CANNOT_CHECK


def test_main_no_devices_is_cannot_check(monkeypatch):
    _attach(monkeypatch, [])
    assert probe.main() == probe.EXIT_CANNOT_CHECK


def test_main_unsupported_arch_measures_nothing_is_cannot_check(monkeypatch):
    """An arch that is neither blackhole nor wormhole is skipped; with nothing else measured the
    run cannot check — it must not claim healthy (0) nor frozen (3)."""
    _attach(monkeypatch, [FakeDevice(0, "grayskull", [LOC])])
    assert probe.main() == probe.EXIT_CANNOT_CHECK


def test_main_all_advancing_is_ok(monkeypatch):
    _fast_window(monkeypatch)
    _attach(monkeypatch, [FakeDevice(0, "blackhole", [LOC])])
    _install(
        monkeypatch,
        {
            probe.BLACKHOLE.port_status: 1,
            probe.BLACKHOLE.rx_link_up: 0x1,
            probe.BLACKHOLE.heartbeat: lambda n: 100 + n,
        },
    )
    assert probe.main() == probe.EXIT_OK


def test_main_frozen_core_is_frozen(monkeypatch):
    _fast_window(monkeypatch)
    _attach(monkeypatch, [FakeDevice(0, "blackhole", [LOC])])
    _install(
        monkeypatch,
        {
            probe.BLACKHOLE.port_status: 1,
            probe.BLACKHOLE.rx_link_up: 0x1,
            probe.BLACKHOLE.heartbeat: 0x1234,
        },
    )
    assert probe.main() == probe.EXIT_FROZEN


def test_main_read_error_skips_core_not_frozen(monkeypatch):
    """A core whose register read raises is skipped, never counted as frozen. With it the only
    core, the run measured nothing -> cannot-check, not a forged frozen verdict."""
    _fast_window(monkeypatch)
    _attach(monkeypatch, [FakeDevice(0, "blackhole", [LOC])])

    def _raise(*_a, **_k):
        raise ValueError("unreadable core")

    monkeypatch.setattr(probe, "read_word_from_device", _raise)
    assert probe.main() == probe.EXIT_CANNOT_CHECK


def test_main_offbus_heartbeat_not_counted(monkeypatch):
    """An up-link core reading all-ones on the heartbeat is off-bus, not frozen: it is not counted
    as measured, so a lone off-bus core resolves to cannot-check."""
    _fast_window(monkeypatch)
    _attach(monkeypatch, [FakeDevice(0, "blackhole", [LOC])])
    _install(
        monkeypatch,
        {
            probe.BLACKHOLE.port_status: 1,
            probe.BLACKHOLE.rx_link_up: 0x1,
            probe.BLACKHOLE.heartbeat: probe.OFF_BUS,
        },
    )
    assert probe.main() == probe.EXIT_CANNOT_CHECK


def test_run_folds_crash_to_cannot_check_never_bare_one(monkeypatch):
    """A bare unhandled exception exits 1, which the wrapper cannot distinguish from a real verdict;
    _run must fold any crash to 77 so a crash is never mistaken for frozen (3) or clean (0)."""

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "main", _boom)
    assert probe._run() == probe.EXIT_CANNOT_CHECK


def test_run_propagates_system_exit(monkeypatch):
    """sys.exit inside main is an explicit exit code, not a crash; _run passes it through untouched."""
    monkeypatch.setattr(probe, "main", lambda: (_ for _ in ()).throw(SystemExit(3)))
    with pytest.raises(SystemExit):
        probe._run()


def test_run_passes_through_main_return(monkeypatch):
    monkeypatch.setattr(probe, "main", lambda: probe.EXIT_OK)
    assert probe._run() == probe.EXIT_OK
