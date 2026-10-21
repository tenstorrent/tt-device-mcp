# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep a host alive across the resets that re-power Tenstorrent silicon.

A mesh-wide reset takes chips off the bus. Each root port above them then
reports Surprise Down, completion timeouts and Advisory Non-Fatal errors. On a 6U Galaxy that error
stream has turned into an AER interrupt flood on their root ports, followed by NMI/RCU stalls and a dead host. This module holds the pieces that keep a reset from
getting there:

* :func:`tt_endpoints` — every Tenstorrent PCI function, its bus, its root port and its /dev id, read
  from sysfs (never from list position, issue #27).
* :class:`AerMask` — mask AER and DPC on root ports for the reset window, then clear their status,
  watch them for a moment and restore only the quiet ones. A port that keeps erroring stays masked
  and is reported, so a flood cannot take the host down.
* :func:`host_reset_gate` — the per-host switch (``TT_DEVICE_MCP_HOST_RESET_GATE``) that refuses an
  automatic reset while chips are off the bus or AER errors are flooding.
* The host-hang latch — :func:`begin_offbus_reset` writes an intent before an automatic reset with
  chips off the bus, and :func:`check_offbus_reset_latch` reads one left by an earlier boot as a
  reset that hung the host, and holds every later off-bus reset on this boot until every chip is
  back on the bus (:func:`expire_offbus_reset_latch`), an operator clears it or the host boots again.

Everything reads and writes through :data:`SYS_ROOT` so tests run against a fake tree.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp import alert
from tt_device_mcp.health.evidence import health_dir, health_event
from tt_device_mcp.health.monitors.heartbeat import dead_chips, read_heartbeats
from tt_device_mcp.health.monitors.hostpci import link_down

_logger = logging.getLogger("tt-device-mcp")

SYS_ROOT = Path("/")
TT_VENDOR = 0x1E52
# A PCI domain has 4 hex digits, or more above 0xffff (an Intel VMD domain reads 10000:...).
_BDF = re.compile(r"^[0-9a-f]{4,}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

# Config-space layout (PCIe base spec). Only what the mask touches.
_CAP_PTR = 0x34
_CAP_ID_PCIE = 0x10
_PCIE_DEVCTL = 0x08  # Device Control: bits 0-3 enable correctable/non-fatal/fatal/UR reporting
_PCIE_DEVSTA = 0x0A  # Device Status: bits 0-3 are RW1C error-detected flags
_EXT_CAP_AER = 0x0001
_EXT_CAP_DPC = 0x001D
_AER_UE_STATUS = 0x04
_AER_UE_MASK = 0x08
_AER_CE_STATUS = 0x10
_AER_CE_MASK = 0x14
_AER_ROOT_CMD = 0x2C  # Root Error Command: bits 0-2 raise the AER interrupt
_AER_ROOT_STATUS = 0x30
_DPC_CTL = 0x06  # DPC Control: bits 0-1 trigger enable, bit 3 interrupt enable
# Every defined uncorrectable (bits 4-5, 12-26) and correctable (0, 6-8, 12-15) error bit.
_UE_ALL = 0x07FFF030
_CE_ALL = 0xF1C1


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _pci_dir() -> Path:
    return SYS_ROOT / "sys/bus/pci/devices"


# ---- topology ---------------------------------------------------------------------------------------


@dataclass
class Endpoint:
    bdf: str
    bus: int
    root_port: Optional[str]
    chip: Optional[int]  # /dev/tenstorrent/<chip>, None when the driver has no node for it
    device: Optional[int] = None  # PCI device id (the architecture), None when unreadable


# What sysfs showed while the chips were on the bus, kept so an off-bus chip's root port and the
# host's architecture are still known once its function is gone (spec 04 I23). Persisted in the
# health dir so a broker that starts with a tray already off the bus knows them too.
TOPOLOGY_FILE = "tt_pci_topology.json"
_SEEN: Optional[dict] = None


def _seen() -> dict:
    global _SEEN
    key = (str(SYS_ROOT), str(health_dir()))
    if _SEEN is None or _SEEN["key"] != key:
        _SEEN = {"key": key, "root_ports": set(), "devices": set()}
        try:
            rec = json.loads((health_dir() / TOPOLOGY_FILE).read_text())
            _SEEN["root_ports"].update(p for p in rec.get("root_ports", []) if isinstance(p, str) and _BDF.match(p))
            _SEEN["devices"].update(d for d in rec.get("devices", []) if isinstance(d, int))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return _SEEN


def _remember(eps: dict) -> None:
    seen = _seen()
    ports = {e.root_port for e in eps.values() if e.root_port}
    devices = {e.device for e in eps.values() if e.device is not None}
    if ports <= seen["root_ports"] and devices <= seen["devices"]:
        return
    seen["root_ports"] |= ports
    seen["devices"] |= devices
    path = health_dir() / TOPOLOGY_FILE
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"root_ports": sorted(seen["root_ports"]), "devices": sorted(seen["devices"])}))
        os.replace(tmp, path)  # a torn write would read back as nothing seen
    except OSError:
        tmp.unlink(missing_ok=True)


class SysfsUnreadable(OSError):
    """The Tenstorrent functions could not be listed from sysfs: no answer, not "none on the bus"."""


def _maybe_tt(dev: Path, chips: dict, upstream: set) -> bool:
    """Whether an entry whose vendor could not be read may be one the guard needs: a function the
    tenstorrent driver owns, a port above a Tenstorrent function seen in this scan, or anything at or
    below a recorded Tenstorrent root port. Any other entry cannot change what the guard does."""
    if dev.name in chips or os.path.basename(os.path.realpath(dev / "driver")) == "tenstorrent":
        return True
    real = Path(os.path.realpath(dev))
    return real.name in upstream or any(p in _seen()["root_ports"] for p in real.parts)


def _scan_endpoints() -> dict:
    """``{bdf: Endpoint}`` read from sysfs. Raises :class:`SysfsUnreadable` when the PCI device list or
    the tenstorrent class list cannot be read, or a Tenstorrent function (or a port above one) cannot
    be judged: its vendor is unreadable, or it has no PCI address. Any other entry the scan cannot
    read is not a Tenstorrent function and is skipped. An entry that vanished mid-scan is simply gone."""
    chips: dict = {}
    cls = SYS_ROOT / "sys/class/tenstorrent"
    try:
        entries = list(cls.iterdir())
    except FileNotFoundError:
        entries = []  # no driver loaded: no chip has a /dev node
    except OSError as exc:
        raise SysfsUnreadable(f"cannot read {cls}: {exc}") from exc
    for entry in entries:
        m = re.search(r"(\d+)$", entry.name)
        if m:
            try:
                chips[os.path.basename(os.path.realpath(entry / "device"))] = int(m.group(1))
            except OSError:
                continue
    out: dict = {}
    unjudged: list = []
    upstream: set = set()  # every PCI function on the sysfs path of a Tenstorrent function
    try:
        entries = sorted(_pci_dir().iterdir())
    except OSError as exc:
        raise SysfsUnreadable(f"cannot read {_pci_dir()}: {exc}") from exc
    for dev in entries:
        try:
            vendor = int((dev / "vendor").read_text().strip(), 16)
        except OSError as exc:
            if not os.path.lexists(dev):
                continue  # removed while we looked
            unjudged.append((dev, exc))
            continue
        except ValueError as exc:
            unjudged.append((dev, exc))
            continue
        if vendor != TT_VENDOR:
            continue
        if not _BDF.match(dev.name):
            raise SysfsUnreadable(f"cannot read {dev}: a Tenstorrent function with no PCI address")
        bdf = dev.name
        parts = [p for p in Path(os.path.realpath(dev)).parts if _BDF.match(p)]
        upstream.update(parts)
        # The root port is the first function above it in its own domain (under Intel VMD the VMD
        # controller, in domain 0000, comes first on the path).
        domain = bdf.rsplit(":", 2)[0]
        above = [p for p in parts[:-1] if p.rsplit(":", 2)[0] == domain]
        root_port = above[0] if above else None
        try:
            device: Optional[int] = int((dev / "device").read_text().strip(), 16)
        except (OSError, ValueError):
            device = None  # no architecture: tray_bus_groups finds no table and the plan refuses
        out[bdf] = Endpoint(
            bdf=bdf, bus=int(bdf.split(":")[-2], 16), root_port=root_port, chip=chips.get(bdf), device=device
        )
    for dev, exc in unjudged:
        if _maybe_tt(dev, chips, upstream):
            raise SysfsUnreadable(f"cannot read {dev / 'vendor'}: {exc}") from exc
    return out


def tt_endpoints() -> Optional[dict]:
    """``{bdf: Endpoint}`` for every Tenstorrent function the kernel lists, or None when sysfs cannot
    be read (never ``{}``, which means every chip is off the bus). The root port is the first PCI
    function on the device's sysfs path; the chip id comes from the tenstorrent class device that
    links to it. Each look is remembered (root ports, architectures) for when the chips are off the
    bus."""
    try:
        out = _scan_endpoints()
    except SysfsUnreadable:
        return None
    _remember(out)
    return out


def record_topology() -> None:
    """Look at the Tenstorrent functions now, so each chip's root port is known once it leaves the
    bus (spec 04 I23). Run at broker start and in every between-jobs check: a sysfs read, and the
    topology file is rewritten only when the look adds a port or an architecture. A look that cannot
    read sysfs keeps what was recorded and leaves a ``pci_topology_unreadable`` event. Never raises."""
    try:
        try:
            _remember(_scan_endpoints())
        except SysfsUnreadable as exc:
            health_event("pci_topology_unreadable", reason=str(exc))
    except Exception:  # noqa: BLE001 - a failed look must not fail the start or the check
        pass


def tt_root_ports(endpoints: Optional[dict] = None) -> list:
    """Every root port with a Tenstorrent function below it now, or below one when an earlier look
    saw it (an off-bus chip's port is the one its re-power floods) — the ports a reset can flood.
    A remembered port that is no longer in sysfs is left out."""
    eps = tt_endpoints() if endpoints is None else endpoints
    present = {e.root_port for e in (eps or {}).values() if e.root_port}
    remembered = {p for p in _seen()["root_ports"] if (_pci_dir() / p).exists()}
    return sorted(present | remembered)


def tray_bus_groups(endpoints: Optional[dict] = None) -> Optional[dict]:
    """tt-smi's ``{tray: bus group}`` table for this host's architecture, or None when it cannot be
    known (tt-smi missing, an unreadable or mixed architecture, or no Tenstorrent function now or
    ever seen). With every chip off the bus the architecture an earlier look saw stands. Imported,
    never copied: the numbering is tt-smi's to define."""
    try:
        from tt_smi.constants import BH_UBB_BUS_IDS, WH_UBB_BUS_IDS
    except Exception:  # noqa: BLE001 - a native extension failing here must not raise into recovery
        return None
    eps = tt_endpoints() if endpoints is None else endpoints
    archs = {e.device for e in eps.values()} if eps else set(_seen()["devices"])
    if archs == {0xB140}:
        return dict(BH_UBB_BUS_IDS)
    if archs == {0x401E}:
        return dict(WH_UBB_BUS_IDS)
    return None


# ---- config space -----------------------------------------------------------------------------------


class _Config:
    """Read and write one function's config space through sysfs (root reads all 4 KiB)."""

    def __init__(self, bdf: str):
        self.bdf = bdf
        self.path = _pci_dir() / bdf / "config"

    def read(self, off: int, size: int) -> int:
        with open(self.path, "rb") as f:
            f.seek(off)
            data = f.read(size)
        if len(data) != size:
            raise OSError(f"{self.bdf}: config read at {off:#x} returned {len(data)} bytes")
        return int.from_bytes(data, "little")

    def write(self, off: int, size: int, value: int) -> None:
        with open(self.path, "r+b", buffering=0) as f:
            f.seek(off)
            f.write(value.to_bytes(size, "little"))

    def cap(self, cap_id: int) -> Optional[int]:
        if not self.read(0x06, 2) & 0x10:
            return None
        ptr, seen = self.read(_CAP_PTR, 1) & 0xFC, 0
        while ptr and seen < 48:
            if self.read(ptr, 1) == cap_id:
                return ptr
            ptr, seen = self.read(ptr + 1, 1) & 0xFC, seen + 1
        return None

    def ext_cap(self, cap_id: int) -> Optional[int]:
        ptr, seen = 0x100, 0
        while ptr and seen < 64:
            hdr = self.read(ptr, 4)
            if hdr in (0, 0xFFFFFFFF):
                return None
            if hdr & 0xFFFF == cap_id:
                return ptr
            ptr, seen = (hdr >> 20) & 0xFFC, seen + 1
        return None


@dataclass
class _PortSave:
    pcie: Optional[int] = None
    aer: Optional[int] = None
    dpc: Optional[int] = None
    devctl: int = 0
    ue_mask: int = 0
    ce_mask: int = 0
    root_cmd: int = 0
    dpc_ctl: int = 0


def _error_status(cfg: _Config, save: _PortSave) -> int:
    """The port's sticky error flags, or 0 when it has no AER: what a quiet port leaves clear."""
    if save.aer is None:
        return 0
    return cfg.read(save.aer + _AER_UE_STATUS, 4) | cfg.read(save.aer + _AER_CE_STATUS, 4)


def _clear_error_status(cfg: _Config, save: _PortSave) -> None:
    if save.aer is not None:
        for off in (_AER_UE_STATUS, _AER_CE_STATUS, _AER_ROOT_STATUS):
            cfg.write(save.aer + off, 4, cfg.read(save.aer + off, 4))
    if save.pcie is not None:
        cfg.write(save.pcie + _PCIE_DEVSTA, 2, cfg.read(save.pcie + _PCIE_DEVSTA, 2) & 0xF)


@dataclass
class AerMask:
    """AER and DPC masked on ``ports`` until :meth:`restore`. ``kept_masked`` names the ports that
    were still erroring at restore time; they stay masked so their flood cannot reach the CPU."""

    ports: list
    saved: dict = field(default_factory=dict)
    kept_masked: list = field(default_factory=list)

    def apply(self) -> "AerMask":
        """Save and mask every port. Raises OSError if any port cannot be masked, after putting
        back the ones already masked: a half-masked host is not one a reset may run on."""
        try:
            for bdf in self.ports:
                cfg = _Config(bdf)
                s = _PortSave(pcie=cfg.cap(_CAP_ID_PCIE), aer=cfg.ext_cap(_EXT_CAP_AER), dpc=cfg.ext_cap(_EXT_CAP_DPC))
                if s.pcie is not None:
                    s.devctl = cfg.read(s.pcie + _PCIE_DEVCTL, 2)
                if s.aer is not None:
                    s.ue_mask = cfg.read(s.aer + _AER_UE_MASK, 4)
                    s.ce_mask = cfg.read(s.aer + _AER_CE_MASK, 4)
                    s.root_cmd = cfg.read(s.aer + _AER_ROOT_CMD, 4)
                if s.dpc is not None:
                    s.dpc_ctl = cfg.read(s.dpc + _DPC_CTL, 2)
                self.saved[bdf] = s
                if s.aer is not None:
                    cfg.write(s.aer + _AER_ROOT_CMD, 4, s.root_cmd & ~0x7)
                    cfg.write(s.aer + _AER_UE_MASK, 4, s.ue_mask | _UE_ALL)
                    cfg.write(s.aer + _AER_CE_MASK, 4, s.ce_mask | _CE_ALL)
                if s.pcie is not None:
                    cfg.write(s.pcie + _PCIE_DEVCTL, 2, s.devctl & ~0xF)
                if s.dpc is not None:
                    cfg.write(s.dpc + _DPC_CTL, 2, s.dpc_ctl & ~0xB)
        except OSError:
            self.restore(quiet_sec=0)
            raise
        return self

    def restore(self, quiet_sec: Optional[float] = None, sleep: Callable[[float], None] = time.sleep) -> list:
        """Clear each port's error flags, wait ``quiet_sec`` and restore the ports that stayed quiet.
        Returns (and records in ``kept_masked``) the ports that errored again."""
        quiet = _env_float("TT_DEVICE_MCP_AER_QUIET_CHECK_SEC", 2.0) if quiet_sec is None else quiet_sec
        for bdf, s in self.saved.items():
            try:
                _clear_error_status(_Config(bdf), s)
            except OSError:
                pass
        if quiet > 0 and self.saved:
            sleep(quiet)
        for bdf, s in list(self.saved.items()):
            cfg = _Config(bdf)
            try:
                if quiet > 0 and _error_status(cfg, s):
                    self.kept_masked.append(bdf)
                    continue
                if s.dpc is not None:
                    cfg.write(s.dpc + _DPC_CTL, 2, s.dpc_ctl)
                if s.pcie is not None:
                    cfg.write(s.pcie + _PCIE_DEVCTL, 2, s.devctl)
                if s.aer is not None:
                    cfg.write(s.aer + _AER_CE_MASK, 4, s.ce_mask)
                    cfg.write(s.aer + _AER_UE_MASK, 4, s.ue_mask)
                    cfg.write(s.aer + _AER_ROOT_CMD, 4, s.root_cmd)
            except OSError:
                self.kept_masked.append(bdf)
        self.saved.clear()
        if self.kept_masked:
            _note_flood()
            health_event("aer_ports_kept_masked", ports=self.kept_masked, host_at_risk=True)
        return self.kept_masked


# ---- the per-host gate ------------------------------------------------------------------------------

_FLOOD_UNTIL = 0.0
_LAST_AER_SAMPLE: Optional[tuple] = None


def _note_flood() -> None:
    global _FLOOD_UNTIL
    _FLOOD_UNTIL = time.monotonic() + _env_float("TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC", 1800)


def flood_window_open() -> bool:
    """A flood was seen within ``TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC``: by :func:`aer_flooding`, or
    by a re-power or reset whose root port kept erroring and stayed masked."""
    return time.monotonic() < _FLOOD_UNTIL


def _aer_total(ports: list) -> int:
    total = 0
    for bdf in ports:
        for name in ("aer_rootport_total_err_cor", "aer_rootport_total_err_nonfatal", "aer_rootport_total_err_fatal"):
            try:
                total += int((_pci_dir() / bdf / name).read_text().split()[0])
            except (OSError, ValueError, IndexError):
                continue
    return total


def _uptime_sec() -> Optional[float]:
    try:
        return float((SYS_ROOT / "proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def aer_flooding(ports: Optional[list] = None) -> bool:
    """True while the Tenstorrent root ports are flooding AER errors, or within the flood window
    (``TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC``, default 1800) after one was seen. A flood is a rate:
    ``TT_DEVICE_MCP_AER_FLOOD_THRESHOLD`` (default 50) new errors per
    ``TT_DEVICE_MCP_AER_FLOOD_PERIOD_SEC`` (default 60) since the previous look, so a steady trickle
    between looks hours apart is not one. The first look after a boot inside the window counts from
    zero over the uptime, so errors that carried on through a reboot are seen."""
    global _LAST_AER_SAMPLE
    ports = tt_root_ports() if ports is None else ports
    now = time.monotonic()
    total = _aer_total(ports)
    prev = _LAST_AER_SAMPLE
    if prev is None:
        up = _uptime_sec()
        if up is not None and up < _env_float("TT_DEVICE_MCP_AER_FLOOD_WINDOW_SEC", 1800):
            prev = (now - up, 0)
    _LAST_AER_SAMPLE = (now, total)
    if prev is not None:
        period = max(_env_float("TT_DEVICE_MCP_AER_FLOOD_PERIOD_SEC", 60), 1.0)
        per_period = (total - prev[1]) * period / max(now - prev[0], period)
        if per_period >= _env_float("TT_DEVICE_MCP_AER_FLOOD_THRESHOLD", 50):
            _note_flood()
    return flood_window_open()


GATE_OFF, GATE_GUARD, GATE_HOLD = "off", "guard", "hold"


def gate_mode() -> str:
    """``TT_DEVICE_MCP_HOST_RESET_GATE``: ``off`` (default) keeps the old reset behaviour, except
    while a port that kept erroring after a re-power or reset holds the flood window open; ``guard``
    masks AER on every Tenstorrent root port around each automatic mesh reset and refuses one while
    AER errors flood; ``hold`` also refuses one while any chip is off the bus. Set per host. A latched
    host-hang hold (:func:`check_offbus_reset_latch`) reads as ``hold`` whatever the setting."""
    if offbus_reset_latched() is not None:
        return GATE_HOLD  # the last off-bus reset hung this host: hold until the chips are back (I25)
    mode = os.environ.get("TT_DEVICE_MCP_HOST_RESET_GATE", GATE_OFF).strip().lower()
    return mode if mode in (GATE_GUARD, GATE_HOLD) else GATE_OFF


def chips_off_bus(expected: int) -> int:
    """How many of ``expected`` chips have no PCI function in sysfs right now. Sysfs that is there
    but cannot be read counts every chip as off, so a ``hold`` gate holds; with no PCI sysfs at all
    it is 0 (the gate then decides on the AER evidence alone). A chip still in sysfs whose ARC
    heartbeat reads all-ones has left the bus too (its BAR reads return nothing), so it counts: a
    ``hold`` box never fires a mesh reset or a sweep over it (spec 04 I24)."""
    eps = tt_endpoints()
    if eps is None:
        return expected if _pci_dir().is_dir() else 0
    # A decision read, not a poll: a lost chip that answers again must not hold a reset (spec 03 I66).
    return max(0, expected - len(eps)) + len(dead_chips(read_heartbeats(probe_lost=True)))


_HOLD_LIFTED: contextvars.ContextVar = contextvars.ContextVar("tt_device_mcp_hold_lifted", default="")


@contextlib.contextmanager
def hold_lifted(why: str):
    """Inside this scope (and the threads it starts) a configured ``hold`` gate reads as ``guard`` for a
    chip off the bus: the AER flood checks still refuse, the I25 host-hang latch still holds. Scoped to
    one escalation pass, so nothing outlives it (spec 04 I33)."""
    token = _HOLD_LIFTED.set(why)
    try:
        yield
    finally:
        _HOLD_LIFTED.reset(token)


def host_reset_gate(off_bus: int) -> tuple:
    """``(allowed, why)`` for an automatic host-affecting reset (the mesh reset) with
    ``off_bus`` chips already off the bus. A refused reset is a rung that did not fire, so the ladder
    above it holds rather than climbing to a reboot or power cycle."""
    if off_bus <= 0:
        expire_offbus_reset_latch(None, None, lambda m: None)  # only the ceiling: no host-pci look here
    elif not _power_cycle_bounds_latch():
        expire_offbus_reset_latch(None, None, _logger.warning, off_bus=off_bus)  # no I27 cycle to end it
    mode = gate_mode()
    if mode == GATE_OFF:
        if flood_window_open():
            # Set by a port the envelope had to leave masked: the next reset is the one that, in the
            # incident, followed a tray re-power into a dead host. Holds whatever the gate's mode.
            return False, "a root port kept erroring AER after the last re-power or reset (flood window open)"
        return True, ""
    if mode == GATE_HOLD and off_bus > 0:
        latch = offbus_reset_latched()
        if latch is not None:
            ceiling = "" if _power_cycle_bounds_latch() else f", it reaches its {latch_max_sec():.0f} s ceiling"
            return False, (
                f"{off_bus} chip(s) off the bus and {_latch_boots(latch)} until every chip is back on the "
                f"bus, the host boots again{ceiling} or an operator runs `{clear_latch_cmd()}`"
            )
        if not _HOLD_LIFTED.get():
            return False, f"{off_bus} chip(s) off the bus and TT_DEVICE_MCP_HOST_RESET_GATE=hold"
        # The off-bus hold's operator hand-off ran out (spec 04 I33): this pass reads `hold` as `guard`.
    if aer_flooding():
        return False, f"AER errors are flooding the Tenstorrent root ports (TT_DEVICE_MCP_HOST_RESET_GATE={mode})"
    return True, ""


# ---- the host-hang latch ----------------------------------------------------------------------------

INTENT_FILE = "offbus_reset_intent.json"
LATCH_FILE = "offbus_reset_hold.json"
CLEAR_LATCH_CMD = "python -m tt_device_mcp.health.recovery.pcie_guard --clear-hang-latch"
_INTENT_OPEN = False


def clear_latch_cmd() -> str:
    """The exact clear command for THIS broker: its interpreter and its health dir, as root. Run
    from another user or venv, the bare module resolves another health dir and clears nothing."""
    return f"sudo {sys.executable} -m tt_device_mcp.health.recovery.pcie_guard --clear-hang-latch --health-dir {health_dir()}"


def _system_health_dir() -> Path:
    return SYS_ROOT / "var/lib/tt-device-broker/health"


def _boot_id() -> str:
    try:
        return (SYS_ROOT / "proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _latch_enabled() -> bool:
    return os.environ.get("TT_DEVICE_MCP_OFFBUS_HANG_LATCH", "1").strip() != "0"


LATCH_MAX_SEC = 21600.0


def latch_max_sec() -> float:
    """``TT_DEVICE_MCP_OFFBUS_HANG_LATCH_MAX_SEC`` (default 21600 s, 6 h): the oldest a host-hang
    hold may grow with every chip on the bus before it lifts without a clean host-pci look, or with
    chips off the bus when no I27 power cycle is configured. The same 6 h as one I27 power-cycle
    interval, so the latch never outlives the next cycle it would allow. Zero, negative or not a
    number reads as the default: the hold is always bounded."""
    v = _env_float("TT_DEVICE_MCP_OFFBUS_HANG_LATCH_MAX_SEC", LATCH_MAX_SEC)
    return v if 0 < v < float("inf") else LATCH_MAX_SEC


def begin_offbus_reset(kind: str, off_bus: int) -> None:
    """Before an automatic reset or re-power fires with ``off_bus`` chips off the bus: write down,
    with this boot's id, that it is about to run. :func:`end_offbus_reset` removes the note once the
    reset and its verify are over. A note a later boot finds means the host died in between."""
    global _INTENT_OPEN
    if off_bus <= 0 or not _latch_enabled():
        return
    path = health_dir() / INTENT_FILE
    rec = {"kind": kind, "off_bus": off_bus, "boot_id": _boot_id(), "at_epoch": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec))
            f.flush()
            os.fsync(f.fileno())
        _INTENT_OPEN = True
    except OSError as exc:
        health_event("offbus_reset_intent_unwritable", error=repr(exc), host_at_risk=True)


def _remove_intent_durably() -> None:
    """Unlink the intent and fsync its directory. The intent itself is fsynced when written, so an
    unlink still in the page cache when the power goes is undone on the next boot (a filesystem
    replays the write and loses the unlink), and that boot reads it as a host crash."""
    path = health_dir() / INTENT_FILE
    path.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def end_offbus_reset() -> None:
    """The reset begun by :func:`begin_offbus_reset` is over and the host is still up."""
    global _INTENT_OPEN
    if _INTENT_OPEN:
        _INTENT_OPEN = False
        try:
            _remove_intent_durably()
        except OSError as exc:
            health_event("offbus_reset_intent_unremovable", error=repr(exc))


def close_offbus_reset_before_host_action(action: str, log: Callable[[str], None]) -> None:
    """The broker is about to reboot or power-cycle the host itself: no off-bus reset is in flight
    (the gate is serialized), so an intent still on disk is not a reset the host may die in. Remove it
    durably, whoever wrote it, so the next boot does not latch the broker's own cycle as a host hang
    (spec 04 I25). Never raises and never stops the action: the next boot's ledger check covers a
    removal that failed."""
    global _INTENT_OPEN
    _INTENT_OPEN = False
    try:
        if (health_dir() / INTENT_FILE).exists():
            _remove_intent_durably()
            health_event("offbus_reset_intent_closed", action=action)
    except OSError as exc:
        log(f"could not remove the off-bus reset intent before the {action}: {exc!r}; going ahead")
        health_event("offbus_reset_intent_unremovable", action=action, error=repr(exc))


def _boot_btime() -> Optional[float]:
    """This boot's start (``/proc/stat`` btime), or None if unreadable."""
    try:
        for ln in (SYS_ROOT / "proc/stat").read_text().splitlines():
            if ln.startswith("btime "):
                return float(ln.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _host_action_after(intent: dict) -> Optional[dict]:
    """The newest reboot or power cycle the broker recorded in its auto-recovery ledger at or after
    ``intent`` was written, on the intent's boot when both name one, that brought this boot up, or
    None. Such a row means the broker took the host down itself after that reset: the intent is not a
    host hang. The row brought this boot up only when this boot started within the attribution window
    after it (plus the pending window for a request marked missed or late, spec 04 I72), the rule
    boot attribution uses everywhere: a request the cycler missed, followed by a real hang and an
    outside power-off later, is no broker action. An unreadable btime attributes nothing."""
    from tt_device_mcp.health.recovery.base import (  # lazy: base is heavy
        AUTO_RECOVERY_LEDGER,
        AUTO_RECOVERY_RANK,
        BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC,
        host_action_pending_sec,
    )

    btime = _boot_btime()
    try:
        at = float(intent.get("at_epoch"))
        lines = (health_dir() / AUTO_RECOVERY_LEDGER).read_text().splitlines()
    except (OSError, TypeError, ValueError):
        return None
    if at != at or btime is None:
        return None
    for ln in reversed(lines):
        try:
            row = json.loads(ln)
            row_at = float(row.get("at_epoch"))
        except (ValueError, TypeError, AttributeError):
            continue
        if row.get("action") not in AUTO_RECOVERY_RANK or row_at < at:
            continue
        if row.get("boot_id") and intent.get("boot_id") and row["boot_id"] != intent["boot_id"]:
            continue
        late = row.get("late") is True or row.get("took_host_down") is False
        window = BOOT_ESCALATION_ATTRIBUTION_WINDOW_SEC + (host_action_pending_sec() if late else 0)
        if not 0 <= btime - row_at <= window:
            continue
        return row
    return None


def _read_latch() -> Optional[dict]:
    try:
        rec = json.loads((health_dir() / LATCH_FILE).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else {}


def _latch_is_this_boots(rec: dict) -> bool:
    """A latch holds only on the boot it was written in (``latched_on_boot``). One from an earlier
    boot, or one that does not say, never holds a rung; with this boot's id unreadable it cannot be
    shown stale, so it holds."""
    current = _boot_id()
    return not current or rec.get("latched_on_boot") == current


def offbus_reset_latched() -> Optional[dict]:
    """The latched host-hang record of this boot, or None when no hold is latched."""
    rec = _read_latch()
    return rec if rec is not None and _latch_is_this_boots(rec) else None


def _power_cycle_bounds_latch() -> bool:
    """A hold with chips off the bus is the I27 power cycle's to end, but only where one is
    configured (``TT_DEVICE_MCP_POWER_CYCLE_CMD``). Without it nothing would ever end that hold."""
    from tt_device_mcp.health.recovery.offbus_power_cycle import power_cycle_argv  # it may import us

    return power_cycle_argv() is not None


def _latch_boots(rec: dict) -> str:
    """Both boots of a latch: the one its reset ran in and the one it holds on."""
    return (
        f"the {rec.get('kind') or 'off-bus reset'} on boot {str(rec.get('boot_id') or '?')[:8]} did not "
        f"finish before the host went down; held on boot {str(rec.get('latched_on_boot') or '?')[:8]}"
    )


def _latch_age(rec: dict) -> float:
    """Seconds since the latch was written: its ``latched_at_epoch``, else the file's mtime, else
    unbounded (a latch whose age cannot be read is past any ceiling)."""
    try:
        at = float(rec["latched_at_epoch"])
    except (KeyError, TypeError, ValueError):
        try:
            at = (health_dir() / LATCH_FILE).stat().st_mtime
        except OSError:
            return float("inf")
    return time.time() - at if at == at else float("inf")


def expire_offbus_reset_latch(
    expected: Optional[int], host_pci_ok: Optional[bool], log: Callable[[str], None], off_bus: int = 0
) -> Optional[str]:
    """Lift this boot's host-hang hold once it guards nothing (spec 04 I25): every one of ``expected``
    chips is on the bus and host-pci is OK, or every chip is on the bus and the latch is older than
    :func:`latch_max_sec`. ``expected`` None means the caller already counted no chip off the bus,
    unless it names ``off_bus`` chips off. While a chip is off the bus or sysfs cannot be read, the
    hold is bounded by the I27 power cycle (the boot after it drops the latch); with no power cycle
    configured it lifts at the same ceiling, paged once, and the gate falls back to its configured
    mode. Returns why it lifted, or None."""
    rec = offbus_reset_latched()
    if rec is None:
        return None
    off = off_bus > 0
    if expected is not None:
        eps = tt_endpoints()
        # A listed endpoint whose link reads down (speed Unknown, width 63) is not back on the bus.
        off = off or expected <= 0 or eps is None or sum(1 for b in eps if not link_down(b, _pci_dir())) < expected
    if off and _power_cycle_bounds_latch():
        return None
    if host_pci_ok is True and expected is not None and not off:
        reason = f"all {expected} chip(s) are back on the bus and host-pci is OK"
    elif _latch_age(rec) >= latch_max_sec():
        reason = f"every chip is on the bus and the hold reached its {latch_max_sec():.0f} s ceiling"
        if off:
            reason = (
                f"chips are still off the bus (or sysfs cannot be read), TT_DEVICE_MCP_POWER_CYCLE_CMD is "
                f"not set, so no power cycle would end the hold, and it reached its {latch_max_sec():.0f} s "
                f"ceiling; automatic resets follow TT_DEVICE_MCP_HOST_RESET_GATE again"
            )
    else:
        return None
    try:
        (health_dir() / LATCH_FILE).unlink(missing_ok=True)
    except OSError as exc:
        log(f"could not remove the host-hang hold that {reason}: {exc!r}; it still holds")
        health_event("offbus_reset_hold_unremovable", latch=rec, reason=reason, error=repr(exc), host_at_risk=True)
        return None
    log(f"the host-hang hold latched on boot {rec.get('latched_on_boot') or '(unknown)'} lifted: {reason}")
    health_event("offbus_reset_hold_expired", latch=rec, boot_id=_boot_id(), reason=reason, chips_off_bus=off)
    if off:  # the latch file is gone, so this pages once per latch
        alert.send_alert(
            {
                "kind": "offbus_reset_hold_expired_off_bus",
                "message": f"{_latch_boots(rec)}; lifted: {reason}",
                "host": socket.gethostname(),
                "host_at_risk": True,
                "severity": "high",
            },
            _logger,
        )
    return reason


def _expire_stale_latch(log: Callable[[str], None]) -> None:
    """Broker start: drop a latch an earlier boot wrote. The host has booted since, so it no longer
    describes this host, and left in place it would read as a hold an operator must clear."""
    rec = _read_latch()
    if rec is None or _latch_is_this_boots(rec):
        return
    try:
        (health_dir() / LATCH_FILE).unlink(missing_ok=True)
    except OSError as exc:
        log(f"could not remove the host-hang hold of an earlier boot: {exc!r}; it is ignored")
    log(f"the host-hang hold latched on boot {rec.get('latched_on_boot') or '(unknown)'} expired with that boot")
    health_event("offbus_reset_hold_expired", latch=rec, boot_id=_boot_id())


def check_offbus_reset_latch(log: Callable[[str], None]) -> Optional[dict]:
    """At broker start: an intent left by another boot is an off-bus reset the host did not survive.
    Latch the gate to ``hold`` for off-bus resets, report it, and return the record. An intent from
    this same boot (the broker restarted, the host did not) is dropped. A latch an earlier boot
    wrote is dropped first: it is scoped to its own boot."""
    _expire_stale_latch(log)
    path = health_dir() / INTENT_FILE
    try:
        intent = json.loads(path.read_text())
    except (OSError, ValueError):
        return offbus_reset_latched()
    path.unlink(missing_ok=True)
    if intent.get("boot_id") == _boot_id() or not _latch_enabled():
        return offbus_reset_latched()
    row = _host_action_after(intent)
    if row is not None:
        log(
            f"the {intent.get('kind')} intent of boot {str(intent.get('boot_id') or '?')[:8]} predates the "
            f"broker's own {row.get('action')} ({row.get('reason')}): not a host hang, no hold latched"
        )
        health_event("offbus_reset_intent_closed", intent=intent, action=row.get("action"), at_boot=True)
        return offbus_reset_latched()
    rec = dict(intent, latched_on_boot=_boot_id(), latched_at_epoch=time.time())
    try:
        (health_dir() / LATCH_FILE).write_text(json.dumps(rec))
    except OSError as exc:
        log(f"could not persist the host-hang hold: {exc!r}; holding for this boot only")
    log(
        f"the {intent.get('kind')} that ran with {intent.get('off_bus')} chip(s) off the bus did not finish "
        f"before the host went down: automatic resets over off-bus chips are HELD until every chip is back "
        f"on the bus with host-pci OK (or for {latch_max_sec():.0f} s with every chip back), the host boots "
        f"again or an operator runs `{clear_latch_cmd()}`"
    )
    health_event("offbus_reset_hold_latched", intent=intent, clear=clear_latch_cmd(), host_at_risk=True)
    return rec


def pcie_guard_at_start(log: Callable[[str], None]) -> Optional[dict]:
    """Broker start: latch the off-bus reset gate when the last boot died inside an off-bus reset
    (:func:`check_offbus_reset_latch`, returned), and record the PCI topology while the mesh is
    still as the host left it."""
    rec = check_offbus_reset_latch(log)
    record_topology()
    return rec


def latch_dirs(health_dir_arg: Optional[str] = None) -> list:
    """Where a broker's latch can be: ``--health-dir`` when given, else this process's health dir
    and the system broker's (``/var/lib/tt-device-broker/health``), which a non-root shell does not
    resolve to on its own."""
    if health_dir_arg:
        return [Path(health_dir_arg)]
    out = [health_dir()]
    if _system_health_dir() not in out:
        out.append(_system_health_dir())
    return out


def clear_offbus_reset_latch(dirs: Optional[list] = None) -> list:
    """Operator step: lift the host-hang hold in each of ``dirs`` (default :func:`latch_dirs`).
    Returns the latch files removed. Raises OSError when one exists but cannot be removed (not root)."""
    cleared = []
    for d in latch_dirs() if dirs is None else dirs:
        path = Path(d) / LATCH_FILE
        if path.exists():
            path.unlink()
            cleared.append(path)
            health_event("offbus_reset_hold_cleared", path=str(path))
    return cleared


def mask_for_mesh_reset(log) -> Optional[AerMask]:
    """Mask every Tenstorrent root port before a mesh reset when the gate is on, else None. A port
    that cannot be masked is logged and the reset goes ahead unmasked, as before the gate. Sysfs that
    cannot be read is logged the same way, and only the recorded root ports are masked."""
    if gate_mode() == GATE_OFF:
        return None
    try:
        eps = _scan_endpoints()
        _remember(eps)
    except SysfsUnreadable as exc:
        log(f"could not list the Tenstorrent root ports before the reset ({exc}); masking only the recorded ones")
        health_event("aer_mask_failed", error=str(exc), host_at_risk=True)
        eps = {}
    try:
        return AerMask(tt_root_ports(eps)).apply()
    except OSError as exc:
        log(f"could not mask AER on the Tenstorrent root ports before the reset: {exc!r}")
        health_event("aer_mask_failed", error=repr(exc), host_at_risk=True)
        return None


def main(argv: Optional[list] = None) -> int:
    """``python -m tt_device_mcp.health.recovery.pcie_guard [--clear-hang-latch [--health-dir DIR]]``:
    with no argument, print the reset gate's mode and any host-hang hold; ``--clear-hang-latch`` clears
    that hold. Reads sysfs only; never touches a device."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--clear-hang-latch"]:
        rest = args[1:]
        if rest and (len(rest) != 2 or rest[0] != "--health-dir"):
            print("usage: pcie_guard --clear-hang-latch [--health-dir DIR]")
            return 2
        dirs = latch_dirs(rest[1] if rest else None)
        try:
            cleared = clear_offbus_reset_latch(dirs)
        except OSError as exc:
            print(f"a host-hang hold is latched but could not be cleared ({exc}); run it as root (sudo)")
            return 1
        if cleared:
            print("host-hang hold cleared: " + ", ".join(str(p) for p in cleared))
        else:
            print("no host-hang hold was latched in " + ", ".join(str(d) for d in dirs))
        return 0
    if args:
        print("usage: pcie_guard [--clear-hang-latch [--health-dir DIR]]")
        return 2
    print(f"gate: {gate_mode()}")
    latch = offbus_reset_latched()
    if latch is not None:
        print(f"host-hang hold LATCHED: {_latch_boots(latch)} ({latch}); clear with `{clear_latch_cmd()}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
