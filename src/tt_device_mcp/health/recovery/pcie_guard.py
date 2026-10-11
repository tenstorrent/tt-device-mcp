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
  reset that hung the host, and holds every later off-bus reset until an operator clears it.

Everything reads and writes through :data:`SYS_ROOT` so tests run against a fake tree.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from tt_device_mcp.health.evidence import health_dir, health_event

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
        return GATE_HOLD  # the last off-bus reset hung this host: hold until an operator clears it
    mode = os.environ.get("TT_DEVICE_MCP_HOST_RESET_GATE", GATE_OFF).strip().lower()
    return mode if mode in (GATE_GUARD, GATE_HOLD) else GATE_OFF


def chips_off_bus(expected: int) -> int:
    """How many of ``expected`` chips have no PCI function in sysfs right now. Sysfs that is there
    but cannot be read counts every chip as off, so a ``hold`` gate holds; with no PCI sysfs at all
    it is 0 (the gate then decides on the AER evidence alone)."""
    eps = tt_endpoints()
    if eps is None:
        return expected if _pci_dir().is_dir() else 0
    return max(0, expected - len(eps))


def host_reset_gate(off_bus: int) -> tuple:
    """``(allowed, why)`` for an automatic host-affecting reset (mesh reset, per-tray re-power) with
    ``off_bus`` chips already off the bus. A refused reset is a rung that did not fire, so the ladder
    above it holds rather than climbing to a reboot or power cycle."""
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
            return False, (
                f"{off_bus} chip(s) off the bus and the off-bus reset before boot {latch.get('boot_id', '?')} "
                f"hung the host; held until an operator runs `{clear_latch_cmd()}`"
            )
        return False, f"{off_bus} chip(s) off the bus and TT_DEVICE_MCP_HOST_RESET_GATE=hold"
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


def end_offbus_reset() -> None:
    """The reset begun by :func:`begin_offbus_reset` is over and the host is still up."""
    global _INTENT_OPEN
    if _INTENT_OPEN:
        _INTENT_OPEN = False
        (health_dir() / INTENT_FILE).unlink(missing_ok=True)


def offbus_reset_latched() -> Optional[dict]:
    """The latched host-hang record, or None when no hold is latched."""
    try:
        return json.loads((health_dir() / LATCH_FILE).read_text())
    except (OSError, ValueError):
        return None


def check_offbus_reset_latch(log: Callable[[str], None]) -> Optional[dict]:
    """At broker start: an intent left by another boot is an off-bus reset the host did not survive.
    Latch the gate to ``hold`` for off-bus resets, report it, and return the record. An intent from
    this same boot (the broker restarted, the host did not) is dropped."""
    path = health_dir() / INTENT_FILE
    try:
        intent = json.loads(path.read_text())
    except (OSError, ValueError):
        return offbus_reset_latched()
    path.unlink(missing_ok=True)
    if intent.get("boot_id") == _boot_id() or not _latch_enabled():
        return offbus_reset_latched()
    rec = dict(intent, latched_on_boot=_boot_id(), latched_at_epoch=time.time())
    try:
        (health_dir() / LATCH_FILE).write_text(json.dumps(rec))
    except OSError as exc:
        log(f"could not persist the host-hang hold: {exc!r}; holding for this boot only")
    log(
        f"the {intent.get('kind')} that ran with {intent.get('off_bus')} chip(s) off the bus did not finish "
        f"before the host went down: automatic resets over off-bus chips are HELD until an operator runs "
        f"`{clear_latch_cmd()}`"
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
    7. rescan the bus, ``reinit`` the chips (POST_RESET ioctl);
    8. restore AER on every port that stayed quiet; one that keeps erroring stays masked.

    Steps 7-8 run even when the quiesce or the pulse raised. ``TT_DEVICE_MCP_TRAY_REPOWER_DRY_RUN=1`` logs the plan
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
            "rescan",
            f"re-init chips {plan.chips}",
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
            _write(SYS_ROOT / "sys/bus/pci/rescan", "1")
            sleep(3)
        except OSError as exc:
            log(f"PCI rescan after the re-power failed: {exc!r}")
        try:
            reinit(plan.chips)
        finally:
            kept = plan.kept_masked = mask.restore(sleep=sleep)
            back = sorted(b for b in plan.functions if (_pci_dir() / b).exists())
            log(
                f"tray re-power done: {len(back)}/{len(plan.functions)} function(s) back"
                + (f"; AER kept masked on erroring port(s) {kept}" if kept else "; AER restored")
            )
            health_event(
                "tray_repower_envelope", trays=plan.trays, back=len(back), of=len(plan.functions), kept_masked=kept
            )
    return plan


def main(argv: Optional[list] = None) -> int:
    """``python -m tt_device_mcp.health.recovery.pcie_guard <bitmap> [chip ...]``: print the plan a
    per-tray re-power would follow. Reads sysfs only; never touches a device."""
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
    if not args:
        print("usage: pcie_guard <bitmap> [chip ...] | --clear-hang-latch [--health-dir DIR]")
        return 2
    plan = plan_tray_repower(int(args[0], 0), [int(a) for a in args[1:]])
    print(f"trays {plan.trays}\nfunctions {plan.functions}\nchips {plan.chips}\nroot ports {plan.root_ports}")
    print(f"mismatch: {plan.mismatch or 'none'}")
    print(f"gate: {gate_mode()}")
    latch = offbus_reset_latched()
    if latch is not None:
        print(f"host-hang hold LATCHED: {latch}; clear with `{clear_latch_cmd()}`")
    return 1 if plan.mismatch else 0


if __name__ == "__main__":
    raise SystemExit(main())
