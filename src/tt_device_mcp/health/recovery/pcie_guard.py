# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep a host alive across the resets that re-power Tenstorrent silicon.

A per-tray BMC re-power or a mesh-wide reset takes chips off the bus. Each root port above them then
reports Surprise Down, completion timeouts and Advisory Non-Fatal errors. On a 6U Galaxy that error
stream has turned into an AER interrupt flood, on the re-powered tray's ports and on a SIBLING tray's
port, followed by NMI/RCU stalls and a dead host. This module holds the pieces that keep a reset from
getting there:

* :func:`tt_endpoints` — every Tenstorrent PCI function, its bus, its root port and its /dev id, read
  from sysfs (never from list position, issue #27).
* :class:`AerMask` — mask AER and DPC on root ports for the reset window, then clear their status,
  watch them for a moment and restore only the quiet ones. A port that keeps erroring stays masked
  and is reported, so a flood cannot take the host down.
* :func:`host_reset_gate` — the per-host switch (``TT_DEVICE_MCP_HOST_RESET_GATE``) that refuses an
  automatic reset while chips are off the bus or AER errors are flooding.
* :func:`safe_tray_repower` — the full envelope around one per-tray BMC re-power, with a dry run.
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
from tt_device_mcp.health.monitors.heartbeat import ALL_ONES, dead_chips, read_heartbeats
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
    """``(allowed, why)`` for an automatic host-affecting reset (mesh reset, per-tray re-power) with
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


def _host_action_after(intent: dict) -> Optional[dict]:
    """The newest reboot or power cycle the broker recorded in its auto-recovery ledger at or after
    ``intent`` was written, on the intent's boot when both name one, or None. Such a row means the
    broker took the host down itself after that reset: the intent is not a host hang."""
    from tt_device_mcp.health.recovery.base import AUTO_RECOVERY_LEDGER, AUTO_RECOVERY_RANK  # lazy: base is heavy

    try:
        at = float(intent.get("at_epoch"))
        lines = (health_dir() / AUTO_RECOVERY_LEDGER).read_text().splitlines()
    except (OSError, TypeError, ValueError):
        return None
    if at != at:
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


# ---- the per-tray re-power envelope -----------------------------------------------------------------


class TrayRepowerRefused(RuntimeError):
    """The envelope refused to cut tray power; nothing was re-powered."""


class TrayRepowerDryRun(TrayRepowerRefused):
    """``TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN=1``: the plan was logged, nothing was touched."""


@dataclass
class TrayPlan:
    bitmap: int
    trays: list
    functions: list  # Tenstorrent functions on the re-powered trays, removed before the pulse
    chips: list  # their /dev ids
    root_ports: list  # every Tenstorrent root port, masked for the window (siblings too)
    mismatch: str = ""
    kept_masked: list = field(default_factory=list)  # ports still erroring after the re-power
    tray_ports: list = field(default_factory=list)  # the re-powered trays' root ports, the only ones rescanned
    siblings_dropped: list = field(default_factory=list)  # chips on other trays that read all-ones after it
    siblings_still_off: list = field(default_factory=list)  # ... and still did after their remove + rescan


def plan_tray_repower(bitmap: int, tray_chip_ids: list) -> TrayPlan:
    """What re-powering ``bitmap`` touches, from sysfs. ``mismatch`` is set when the caller's chip ids
    and the trays' buses disagree (a mis-mapped tray, issue #27): firing then would re-power a
    healthy tray. It is also set when sysfs cannot be read: that is no answer, not every chip off the
    bus, and the cross-check cannot run. Chips with no /dev node are off the bus and cannot be checked."""
    trays = [i + 1 for i in range(8) if bitmap >> i & 1]
    try:
        eps = _scan_endpoints()
    except SysfsUnreadable as exc:
        return TrayPlan(
            bitmap=bitmap,
            trays=trays,
            functions=[],
            chips=[],
            root_ports=[],
            mismatch=f"{exc}; refusing the re-power of trays {trays}: the tray map cannot be checked",
        )
    _remember(eps)
    groups = tray_bus_groups(eps)
    plan = TrayPlan(bitmap=bitmap, trays=trays, functions=[], chips=[], root_ports=tt_root_ports(eps))
    plan.tray_ports = tray_root_ports(trays, eps, groups)
    if not eps:
        # Every chip is off the bus: no healthy tray can be cut, and there is nothing to cross-check.
        # Refusing here would leave an all-off mesh with no tray re-power before the power cycle.
        return plan
    if not groups or any(t not in groups for t in trays):
        plan.mismatch = f"no tt-smi tray table covers trays {trays} on this host"
        return plan
    wanted = {groups[t] for t in trays}
    on_trays = [e for e in eps.values() if e.bus & 0xF0 in wanted]
    plan.functions = sorted(e.bdf for e in on_trays)
    plan.chips = sorted(e.chip for e in on_trays if e.chip is not None)
    given = {int(c) for c in tray_chip_ids}
    elsewhere = sorted(e.chip for e in eps.values() if e.chip in given and e.bus & 0xF0 not in wanted)
    missing = sorted(set(plan.chips) - given)
    if elsewhere or missing:
        plan.mismatch = (
            f"tray map disagrees with sysfs for trays {trays}: chip(s) {elsewhere} sit on other trays, "
            f"chip(s) {missing} on these trays were not named"
        )
    return plan


def _secondary_buses(port: str) -> list:
    """The buses below a root port, from its ``pci_bus`` children (there with or without a chip on them)."""
    try:
        return sorted(int(b.name.split(":")[-1], 16) for b in (_pci_dir() / port / "pci_bus").iterdir())
    except (OSError, ValueError):
        return []


def tray_root_ports(trays: list, endpoints: Optional[dict] = None, groups: Optional[dict] = None) -> list:
    """The root ports of ``trays``: those above a Tenstorrent function on them now, and every Tenstorrent
    root port (present or remembered) whose bus below falls in their tt-smi bus group, which still finds
    the port of a chip that is off the bus. ``[]`` when no tray table is known."""
    eps = (tt_endpoints() or {}) if endpoints is None else endpoints
    groups = tray_bus_groups(eps) if groups is None else groups
    if not groups:
        return []
    wanted = {groups[t] for t in trays if t in groups}
    ports = {e.root_port for e in eps.values() if e.root_port and e.bus & 0xF0 in wanted}
    ports |= {p for p in tt_root_ports(eps) if any(b & 0xF0 in wanted for b in _secondary_buses(p))}
    return sorted(ports)


def rescan_root_ports(ports: list, log: Callable[[str], None]) -> list:
    """Rescan only the bus below each of ``ports`` (the kernel's ``pci_bus/<bus>/rescan``), never the
    whole host: a global rescan also walks every other tray's ports, and after each per-tray re-power
    on a 6U Galaxy the slot-6 chip of every other tray was left reading all-ones. Returns the ports
    rescanned; one that cannot be is logged and skipped."""
    done = []
    for port in ports:
        try:
            buses = sorted((_pci_dir() / port / "pci_bus").iterdir())
        except OSError as exc:
            log(f"no scoped rescan for root port {port}: {exc!r}")
            continue
        for bus in buses:
            try:
                _write(bus / "rescan", "1")
            except OSError as exc:
                log(f"rescan of bus {bus.name} below {port} failed: {exc!r}")
                continue
            if port not in done:
                done.append(port)
    return done


def rescan_trays(trays: list, log: Callable[[str], None]) -> list:
    """:func:`rescan_root_ports` for the root ports of ``trays``. ``[]`` when none could be rescanned."""
    return rescan_root_ports(tray_root_ports(trays), log)


def _heartbeats() -> dict:
    """``{chip: ARC heartbeat}`` from the tenstorrent class: a sysfs read, the driver answers it. A chip
    whose value cannot be read is left out."""
    out: dict = {}
    try:
        entries = list((SYS_ROOT / "sys/class/tenstorrent").iterdir())
    except OSError:
        return out
    for entry in entries:
        m = re.search(r"(\d+)$", entry.name)
        if not m:
            continue
        try:
            out[int(m.group(1))] = int((entry / "tt_heartbeat").read_text().strip())
        except (OSError, ValueError):
            continue
    return out


def _live_siblings(plan: TrayPlan) -> dict:
    """``{chip: Endpoint}`` for the chips NOT on the re-powered trays whose heartbeat reads live now: the
    ones a re-power must not take off the bus. Empty when sysfs cannot be read."""
    eps = tt_endpoints() or {}
    beats = _heartbeats()
    return {
        e.chip: e
        for e in eps.values()
        if e.chip is not None and e.bdf not in plan.functions and beats.get(e.chip, ALL_ONES) != ALL_ONES
    }


def _recover_siblings(plan: TrayPlan, live: dict, log: Callable[[str], None], sleep: Callable[[float], None]) -> None:
    """After the pulse: a chip on another tray that read live before it and all-ones now gets ONE remove
    and a rescan of its own root port. One still all-ones after that is left to the caller (the walk's
    regression check hands it to the mesh reset)."""
    if not live:
        return
    beats = _heartbeats()
    dropped = sorted(c for c in live if beats.get(c) == ALL_ONES)
    if not dropped:
        return
    plan.siblings_dropped = dropped
    log(f"tray re-power of trays {plan.trays} left chip(s) {dropped} on other trays reading all-ones; remove + rescan")
    for c in dropped:
        try:
            _write(_pci_dir() / live[c].bdf / "remove", "1")
        except OSError as exc:
            log(f"could not remove {live[c].bdf} (chip {c}): {exc!r}")
    rescan_root_ports(sorted({live[c].root_port for c in dropped if live[c].root_port}), log)
    sleep(3)
    beats = _heartbeats()
    by_bdf = {e.bdf: e.chip for e in (tt_endpoints() or {}).values()}
    plan.siblings_still_off = [
        c for c in dropped if beats.get(by_bdf.get(live[c].bdf), ALL_ONES) == ALL_ONES  # type: ignore[arg-type]
    ]
    health_event(
        "tray_repower_sibling_offbus",
        trays=plan.trays,
        chips=dropped,
        functions=[live[c].bdf for c in dropped],
        still_off=plan.siblings_still_off,
        host_at_risk=True,
    )


# Set by :func:`disable_tray_rung` once a per-tray re-power took chips on other trays off the bus
# (``ubb_reset_regressed_offbus``): the per-tray rung (the verify-between walk and the sweep's tray
# re-power) is declined on this host for ``TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS`` (default 7). Written to
# ``TRAY_RUNG_FILE`` in the health dir with the host name and time, so a broker restart and a reboot or
# power cycle both keep it: the UBB that regressed is still the same one after a cycle (measured: the
# rung re-armed on each new boot and the same re-power took 20 more chips off three times in an hour).
# A record from another host or older than the window is dropped. An operator clears it after a repair
# with :func:`clear_tray_rung_cmd`; a running broker sees the file gone and re-arms the rung.
TRAY_RUNG_FILE = "ubb_tray_rung_disabled.json"
TRAY_RUNG_LATCH_DEFAULT_DAYS = 7.0
_TRAY_RUNG_DISABLED: Optional[dict] = None
_TRAY_RUNG_AT = 0.0  # when the in-memory record was latched (epoch s)
_TRAY_RUNG_PERSISTED = False  # the in-memory record is backed by TRAY_RUNG_FILE


def clear_tray_rung_cmd() -> str:
    """The exact command that re-arms the per-tray rung on THIS broker after a hardware repair (its
    interpreter and health dir, as root), as :func:`clear_latch_cmd` does for the host-hang latch."""
    return (
        f"sudo {sys.executable} -m tt_device_mcp.health.recovery.pcie_guard --clear-tray-rung-latch "
        f"--health-dir {health_dir()}"
    )


def clear_tray_rung_latch(dirs: Optional[list] = None) -> list:
    """Operator step: re-arm the per-tray rung in each of ``dirs`` (default :func:`latch_dirs`), e.g.
    after the UBB whose re-power regressed was repaired. Returns the files removed. Raises OSError when
    one exists but cannot be removed (not root)."""
    global _TRAY_RUNG_DISABLED, _TRAY_RUNG_PERSISTED
    cleared = []
    for d in latch_dirs() if dirs is None else dirs:
        path = Path(d) / TRAY_RUNG_FILE
        if path.exists():
            path.unlink()
            cleared.append(path)
            health_event("ubb_tray_rung_cleared", path=str(path), by="operator")
    if cleared:
        _TRAY_RUNG_DISABLED, _TRAY_RUNG_PERSISTED = None, False
    return cleared


def tray_rung_latch_sec() -> float:
    """How long a recorded tray re-power regression declines the per-tray rung on this host. A malformed
    or non-positive ``TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS`` reads as the default."""
    try:
        days = float(os.environ.get("TT_DEVICE_MCP_TRAY_RUNG_LATCH_DAYS", "") or TRAY_RUNG_LATCH_DEFAULT_DAYS)
    except ValueError:
        days = TRAY_RUNG_LATCH_DEFAULT_DAYS
    return (days if days > 0 else TRAY_RUNG_LATCH_DEFAULT_DAYS) * 86400.0


def disable_tray_rung(**why) -> None:
    """Decline the per-tray rung on this host for :func:`tray_rung_latch_sec` (``ubb_tray_rung_disabled``,
    once). The record goes to the health dir with the host name and time, so it survives a broker
    restart, a reboot and a power cycle."""
    global _TRAY_RUNG_DISABLED, _TRAY_RUNG_AT, _TRAY_RUNG_PERSISTED
    if tray_rung_disabled() is not None:
        return
    rec = {"why": dict(why), "boot_id": _boot_id(), "host": socket.gethostname(), "at_epoch": time.time()}
    _TRAY_RUNG_DISABLED, _TRAY_RUNG_AT, _TRAY_RUNG_PERSISTED = dict(why), rec["at_epoch"], False
    try:
        path = health_dir() / TRAY_RUNG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec))
            f.flush()
            os.fsync(f.fileno())
        _TRAY_RUNG_PERSISTED = True
    except OSError as exc:
        health_event("ubb_tray_rung_disabled_unwritable", error=repr(exc), host_at_risk=True)
    health_event(
        "ubb_tray_rung_disabled",
        **why,
        boot_id=rec["boot_id"],
        latch_days=tray_rung_latch_sec() / 86400.0,
        clear_with=clear_tray_rung_cmd(),
        host_at_risk=True,
    )


def tray_rung_disabled() -> Optional[dict]:
    """Why the per-tray rung is disabled on this host, or None while it is armed. Reads the record an
    earlier broker process wrote, on this boot or an earlier one: a power cycle does not repair the UBB
    whose re-power regressed. A record from another host, or older than :func:`tray_rung_latch_sec`, is
    removed and never counts. One with no time of its own is aged by the file's mtime; one whose age cannot
    be read at all cannot be shown stale, so it counts: the hold it routes to is the bounded I27 hand-off."""
    global _TRAY_RUNG_DISABLED, _TRAY_RUNG_AT, _TRAY_RUNG_PERSISTED
    path = health_dir() / TRAY_RUNG_FILE
    if _TRAY_RUNG_DISABLED is not None:
        if _TRAY_RUNG_PERSISTED and not path.exists():
            # An operator cleared it (:func:`clear_tray_rung_cmd`) while this broker ran: re-arm now.
            health_event("ubb_tray_rung_cleared", path=str(path), by="file_removed", latch=_TRAY_RUNG_DISABLED)
            _TRAY_RUNG_DISABLED, _TRAY_RUNG_PERSISTED = None, False
            return None
        if time.time() - _TRAY_RUNG_AT < tray_rung_latch_sec():
            return _TRAY_RUNG_DISABLED
        _TRAY_RUNG_DISABLED = None
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    current = _boot_id()
    at = rec.get("at_epoch") if isinstance(rec, dict) else None
    if not isinstance(at, (int, float)):  # a record from before the time was kept: the file's own
        try:
            at = path.stat().st_mtime
        except OSError:
            at = None
    age = time.time() - at if isinstance(at, (int, float)) else None
    host = rec.get("host") if isinstance(rec, dict) else None
    stale = (host and host != socket.gethostname()) or (age is not None and age >= tray_rung_latch_sec())
    if not isinstance(rec, dict) or stale:
        path.unlink(missing_ok=True)
        health_event("ubb_tray_rung_rearmed", latch=rec, boot_id=current, age_sec=age)
        return None
    why = rec.get("why")
    _TRAY_RUNG_DISABLED = dict(why) if isinstance(why, dict) else {}
    _TRAY_RUNG_AT = at if age is not None else time.time()
    _TRAY_RUNG_PERSISTED = True
    health_event(
        "ubb_tray_rung_disabled_restored",
        **_TRAY_RUNG_DISABLED,
        boot_id=current,
        latched_boot_id=rec.get("boot_id"),
        age_sec=age,
        clear_with=clear_tray_rung_cmd(),
        host_at_risk=True,
    )
    return _TRAY_RUNG_DISABLED


# The last per-tray re-power of this process, for :func:`latch_tray_pulse_drop`: chips on the trays it
# did NOT re-power that leave the bus within ``TRAY_PULSE_WINDOW_SEC`` of it, all at in-tray position 5
# (chips 5/13/21/29 on a Galaxy), are the cross-tray signature some UBBs show a minute after a pulse.
# The drop often lands after the pulse's own hold was released, in a new hold episode, so the match spans
# episodes inside the window. The record names the pulse's episode and trays, so an operator who finds
# the drop was someone else's (e.g. an outside load test) re-arms the rung with clear_tray_rung_cmd().
TRAY_PULSE_WINDOW_SEC = 900.0
TRAY_PULSE_POSITION = 5
_LAST_TRAY_PULSE: Optional[dict] = None


def note_tray_pulse(trays, tray_map: Optional[dict], off_before, episode: str = "") -> None:
    """A per-tray re-power of ``trays`` was fired with ``off_before`` (chip ids) off the bus, in the hold
    episode ``episode`` (its onset; empty when unknown)."""
    global _LAST_TRAY_PULSE
    if not tray_map:
        return
    _LAST_TRAY_PULSE = {
        "trays": sorted(int(t) for t in trays),
        "tray_map": {int(t): sorted(int(c) for c in chips) for t, chips in tray_map.items()},
        "off_before": {str(c) for c in off_before},
        "at": time.monotonic(),
        "episode": episode or "",
    }


def latch_tray_pulse_drop(off_now, episode: str = "") -> Optional[dict]:
    """Disable the per-tray rung (:func:`disable_tray_rung`) when chips that were on the bus before the
    last tray re-power, on trays it did not re-power, are now off and every one of them sits at in-tray
    position 5. Only that signature counts: a mesh reset fired after the pulse drops other chips too.
    The record names the pulse's hold episode, and ``episode`` (the hold the drop was seen in) when it
    is another one. Returns why it latched, or None."""
    pulse = _LAST_TRAY_PULSE
    if pulse is None or time.monotonic() - pulse["at"] > TRAY_PULSE_WINDOW_SEC:
        return None
    new_off = {str(c) for c in off_now} - pulse["off_before"]
    elsewhere = {
        c: pos
        for tray, chips in pulse["tray_map"].items()
        if tray not in pulse["trays"]
        for pos, c in enumerate(chips)
        if str(c) in new_off
    }
    if not elsewhere or set(elsewhere.values()) != {TRAY_PULSE_POSITION}:
        return None
    why = {"tray": pulse["trays"], "new_off": [str(c) for c in sorted(elsewhere)], "signature": "position5"}
    if pulse.get("episode"):
        why["episode"] = pulse["episode"]
    if episode and episode != pulse.get("episode"):
        why["seen_in"] = episode
    health_event("ubb_reset_regressed_offbus", **why, host_at_risk=True)
    disable_tray_rung(**why)
    return why


def _tray_holders(chips: list) -> dict:
    """``{chip: [pid, ...]}`` for the tray's chips that a process still holds open (tt-kmd's own
    record). A chip the driver does not publish has no entry."""
    out = {}
    for c in chips:
        try:
            pids = [int(p) for p in (SYS_ROOT / f"proc/driver/tenstorrent/{c}/pids").read_text().split()]
        except (OSError, ValueError):
            continue
        pids = [p for p in pids if p != os.getpid()]
        if pids:
            out[c] = pids
    return out


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def safe_tray_repower(
    bitmap: int,
    tray_chip_ids: list,
    fire: Callable[[], None],
    log: Callable[[str], None],
    *,
    quiesce: Callable[[list], None] = lambda chips: None,
    reinit: Callable[[list], None] = lambda chips: None,
    sleep: Callable[[float], None] = time.sleep,
) -> TrayPlan:
    """Re-power the trays in ``bitmap`` without letting the host see the fallout:

    1. refuse if the tray map disagrees with sysfs (a wrong tray would be cut) or sysfs cannot be
       read; with every chip off the bus there is no healthy tray to cut and nothing to
       cross-check, so it goes ahead;
    2. wait up to ``TT_DEVICE_MCP_TRAY_REPOWER_HOLDER_WAIT_SEC`` (default 10) for every process to
       let go of the tray's chips, else refuse;
    3. mask AER and DPC on EVERY Tenstorrent root port, the sibling trays' and the off-bus chips'
       included; a port that cannot be masked refuses the fire before any chip is touched;
    4. ``quiesce`` the chips (tt-smi's USER_RESET ioctl);
    5. remove the tray's PCI functions so nothing in the kernel touches them while unpowered;
    6. ``fire`` the BMC pulse (it also waits out the settle);
    7. rescan only the tray's root ports (a global rescan only when none is known), ``reinit`` the
       chips (POST_RESET ioctl);
    8. heartbeat-check the other trays' chips: one that read live before the pulse and all-ones now
       gets one remove and a rescan of its own root port;
    9. restore AER on every port that stayed quiet; one that keeps erroring stays masked.

    Steps 7-9 run even when the quiesce or the pulse raised. ``TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN=1`` logs the plan
    and raises :class:`TrayRepowerDryRun` before step 2."""
    plan = plan_tray_repower(bitmap, tray_chip_ids)
    log(
        f"tray re-power plan: trays {plan.trays} (bitmap {bitmap:#04x}); functions {plan.functions}; "
        f"chips {plan.chips}; AER/DPC masked on {plan.root_ports}"
    )
    if os.environ.get("TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN", "").strip() == "1":
        steps = [
            "wait for holders",
            f"mask AER/DPC on {plan.root_ports}",
            f"quiesce chips {plan.chips}",
            f"remove {plan.functions}",
            f"fire BMC re-power {bitmap:#04x}",
            f"rescan {plan.tray_ports or 'the bus'}",
            f"re-init chips {plan.chips}",
            "heartbeat-check the other trays",
            "restore AER on quiet ports",
        ]
        log("tray re-power DRY RUN, nothing touched: " + " -> ".join(steps))
        health_event("tray_repower_dry_run", trays=plan.trays, functions=plan.functions, mismatch=plan.mismatch)
        raise TrayRepowerDryRun(plan.mismatch or "dry run")
    if plan.mismatch:
        health_event("tray_repower_refused", trays=plan.trays, reason=plan.mismatch, host_at_risk=True)
        raise TrayRepowerRefused(plan.mismatch)
    wait = _env_float("TT_DEVICE_MCP_TRAY_REPOWER_HOLDER_WAIT_SEC", 10)
    deadline = time.monotonic() + wait
    holders = _tray_holders(plan.chips)
    while holders and time.monotonic() < deadline:
        sleep(0.5)
        holders = _tray_holders(plan.chips)
    if holders:
        why = f"chip(s) still held open after {wait:.0f}s: {holders}"
        health_event("tray_repower_refused", trays=plan.trays, reason=why, holders=holders)
        raise TrayRepowerRefused(why)
    live = _live_siblings(plan)
    try:
        mask = AerMask(plan.root_ports).apply()
    except OSError as exc:
        why = f"could not mask AER on the Tenstorrent root ports: {exc!r}"
        health_event("tray_repower_refused", trays=plan.trays, reason=why, host_at_risk=True)
        raise TrayRepowerRefused(why) from exc
    try:
        quiesce(plan.chips)
        for bdf in plan.functions:
            try:
                _write(_pci_dir() / bdf / "remove", "1")
            except OSError as exc:
                log(f"could not remove {bdf} before the re-power: {exc!r}")
        fire()
    finally:
        try:
            if not rescan_root_ports(plan.tray_ports, log):
                log(f"no root port of trays {plan.trays} could be rescanned on its own; rescanning the whole bus")
                _write(SYS_ROOT / "sys/bus/pci/rescan", "1")
            sleep(3)
        except OSError as exc:
            log(f"PCI rescan after the re-power failed: {exc!r}")
        try:
            reinit(plan.chips)
        finally:
            try:
                _recover_siblings(plan, live, log, sleep)
            except Exception as exc:  # noqa: BLE001 - the AER restore below must still run
                log(f"checking the other trays after the re-power failed: {exc!r}")
            kept = plan.kept_masked = mask.restore(sleep=sleep)
            back = sorted(b for b in plan.functions if (_pci_dir() / b).exists())
            log(
                f"tray re-power done: {len(back)}/{len(plan.functions)} function(s) back"
                + (f"; AER kept masked on erroring port(s) {kept}" if kept else "; AER restored")
                + (f"; other trays' chip(s) {plan.siblings_dropped} dropped" if plan.siblings_dropped else "")
            )
            health_event(
                "tray_repower_envelope",
                trays=plan.trays,
                back=len(back),
                of=len(plan.functions),
                kept_masked=kept,
                siblings_dropped=plan.siblings_dropped,
                siblings_still_off=plan.siblings_still_off,
            )
    return plan


def main(argv: Optional[list] = None) -> int:
    """``python -m tt_device_mcp.health.recovery.pcie_guard <bitmap> [chip ...]``: print the plan a
    per-tray re-power would follow. Reads sysfs only; never touches a device."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--clear-tray-rung-latch"]:
        rest = args[1:]
        if rest and (len(rest) != 2 or rest[0] != "--health-dir"):
            print("usage: pcie_guard --clear-tray-rung-latch [--health-dir DIR]")
            return 2
        dirs = latch_dirs(rest[1] if rest else None)
        try:
            cleared = clear_tray_rung_latch(dirs)
        except OSError as exc:
            print(f"the per-tray rung is latched off but could not be re-armed ({exc}); run it as root (sudo)")
            return 1
        if cleared:
            print("per-tray rung re-armed: " + ", ".join(str(p) for p in cleared))
        else:
            print("the per-tray rung was not latched off in " + ", ".join(str(d) for d in dirs))
        return 0
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
    if not args:
        print(
            "usage: pcie_guard <bitmap> [chip ...] | --clear-hang-latch [--health-dir DIR] "
            "| --clear-tray-rung-latch [--health-dir DIR]"
        )
        return 2
    plan = plan_tray_repower(int(args[0], 0), [int(a) for a in args[1:]])
    print(f"trays {plan.trays}\nfunctions {plan.functions}\nchips {plan.chips}\nroot ports {plan.root_ports}")
    print(f"mismatch: {plan.mismatch or 'none'}")
    print(f"gate: {gate_mode()}")
    latch = offbus_reset_latched()
    if latch is not None:
        print(f"host-hang hold LATCHED: {_latch_boots(latch)} ({latch}); clear with `{clear_latch_cmd()}`")
    rung = tray_rung_disabled()
    if rung is not None:
        print(f"per-tray rung LATCHED OFF ({rung}); re-arm after a repair with `{clear_tray_rung_cmd()}`")
    return 1 if plan.mismatch else 0


if __name__ == "__main__":
    raise SystemExit(main())
