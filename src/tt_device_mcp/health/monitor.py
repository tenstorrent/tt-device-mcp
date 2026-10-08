# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""HealthMonitor: the aggregate that owns one probe pass and the readings blackboard.

Every check that decides whether the mesh is usable — the tt-smi enumeration snapshot, the
fabric traffic pass, the passive eth-core heartbeat read, and the board-type cache the reset
command needs — used to live as free functions and module globals on ``server.py``. That left
their state (the board-type cache, the last-real-fabric-verdict latch, the in-flight fabric
check handle a dropping chip must be able to kill) scattered across the module with no single
owner. ``HealthMonitor`` is that owner: constructed once as a module-level singleton (see
``server.health_monitor``), never per gate pass — a fresh instance would lose the fabric latch
that stops a recovered-on-ARC hold from lifting onto an unverified fabric.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

from tt_device_mcp import metrics
from tt_device_mcp.constants import (
    ETH_POST_JOB_TIMEOUT_SEC,
    FABRIC_CHECK_CANNOT_CHECK_RC,
    FABRIC_CHECK_TIMEOUT_SEC,
)
from tt_device_mcp.health.core import HealthState, Observation, Verdict
from tt_device_mcp.health.evidence import health_dir, health_event
from tt_device_mcp.health.monitors import eth, fabric, hostpci


def _verdict_label(ok: Optional[bool]) -> str:
    """The tri-state ``(ok, unhealthy, could-not-check)`` every probe here returns, in
    :data:`metrics.VERDICTS` terms."""
    return "healthy" if ok is True else "skipped" if ok is None else "unhealthy"


# Every board type tt-smi cannot identify, including the literal it emits when an ARC read fails.
# Reading these as "not a Galaxy" is how a degraded Galaxy gets the reset that cannot recover it.
_UNIDENTIFIED_BOARDS = ("", "n/a")


def _normalized_board(raw) -> str:
    """A snapshot board type reduced to what tt-smi itself compares: case-folded, without the
    " L"/" R" suffix tt-smi appends to its log copy only. Whitespace comes off before the suffix as
    well as after, or a padded value keeps the suffix and reads as some other board entirely."""
    board = str(raw).strip()
    if board.endswith((" L", " R")):
        board = board[:-2].strip()
    return board.lower()


class HealthMonitor:
    """Runs the probe pass and holds its state: the board-type snapshot cache, the fabric probe's
    last-real-verdict latch, the in-flight fabric-check process handle, and the last ``readings``
    a pass produced. ``deps`` is the same late-bound accessor bag :class:`Recovery` uses (see
    ``tt_device_mcp.health.recovery.RecoveryDeps``) — ``update()`` is what reaches into it for the
    heartbeat probe (``heartbeat_supported``/``heartbeat_verdict``) and ``set_device_op_detail``,
    the same seam :class:`Recovery` has always used, so a test that monkeypatches a server-owned
    dep controls this probe pass too.
    """

    CHIP_BASELINE_FILE = "chip_baseline.json"
    ETH_LINK_BASELINE_FILE = "eth_link_baseline.json"

    def __init__(self, deps) -> None:
        self._deps = deps
        self._begin_action_row = deps.begin_action_row
        self._write_action_log = deps.write_action_log
        self._terminate_process_group = deps.terminate_process_group
        self._logger = deps.logger
        # The SAME /dev/tenstorrent node enumeration the gate, the idle escalators, and the reset
        # routes already use (server._present_chip_indices) — update()'s present-chip count must
        # agree with theirs on every host, including one whose driver exposes no ARC heartbeat at
        # all (heartbeat_supported() permanently False), where a heartbeat-derived count would
        # silently read 0 present chips and neuter expected()'s enumeration-count check.
        self._present_chip_indices = deps.present_chip_indices

        # Board types from the health gate's own tt-smi snapshot. None until one has been read:
        # the reset argv must never issue a snapshot of its own — that would be a device init on
        # the event loop, and on the reset path it would land on silicon the gate has just
        # declared unhealthy.
        self._board_types: Optional[list] = None
        # Per-chip PCI bus ids from the same snapshot, indexed by chip id. Banked for the same
        # reason and with the same lifetime as the board types, but for a stricter one: a chip
        # that has left the bus is absent from every later snapshot AND from sysfs, so the tray
        # rung that needs its bus (spec 04 I16) can only ever read one taken before the drop.
        self._bus_ids: Optional[list] = None
        self._glx_cache: Optional[tuple] = None

        # The once-per-process dedup for no-verdict skip journaling: the fabric and eth-heartbeat
        # checks run on every gate and every idle relift, so a skip that reflects a STATIC
        # condition — the command unset, the tool not installed — must journal ONCE, not on every
        # verify until the record is buried under its own repeats.
        self._skip_events_journaled: set[tuple[str, str]] = set()

        # The last REAL fabric verdict (True healthy / False unhealthy); None until one runs. Kept
        # across checks so the read-only relift can refuse to lift a recovered-on-ARC hold back
        # onto a fabric whose last verdict was unhealthy — enum+ARC recover before the eth cores
        # do, and lifting there admits the next tenant onto a fabric that still cannot move data.
        # A skip (None) never overwrites it: not knowing is not the same as knowing it is fit.
        self.last_fabric_ok: Optional[bool] = None
        self.last_fabric_output: str = ""
        # The in-flight fabric traffic pass. It maps every chip's BARs, so when a chip leaves the
        # bus it is the likeliest thing still reading a dead address — and `pci remove` does not
        # revoke its mapping. Tracked so a dead chip can kill it (see server._kill_device_holders).
        self.fabric_check_proc: asyncio.subprocess.Process | None = None

        # The last pass's readings blackboard. None until update() runs once.
        self._readings: Optional[HealthState] = None

    def status(self) -> Optional[HealthState]:
        """The last probe pass, or None before one has ever run. Never blocks, never probes —
        the read side of :meth:`update`, for the FSM and Recovery to consult at will."""
        return self._readings

    async def update(
        self,
        phase: str,
        *,
        run_fabric: bool,
        force_fabric: bool = False,
        run_eth: bool = False,
        indices: Optional[list] = None,
        expected: Optional[int] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> HealthState:
        """Run one probe pass — heartbeat+pci always, then eth, then the fabric traffic pass
        when ``run_fabric``/``force_fabric`` — and store it as the new ``readings`` blackboard.

        ``run_eth`` asks for the passive eth read WITHOUT the traffic pass: the clean post-job gate
        on a host whose eth rung is armed (spec 03 I30). The read is bounded to
        ``ETH_POST_JOB_TIMEOUT_SEC`` there. A frozen verdict stops the pass as usual; a read that
        reached no verdict (its own timeout, a crash, a could-not-check) runs the fabric traffic
        pass in this same pass, because on an armed host that read has answered before and a
        read that now cannot is the stuck-read shape a fabric failure follows. Pass it only when
        the rung is armed: a disarmed reader always skips, so ``run_eth`` there would buy the
        traffic pass on every call.

        Mirrors ``Recovery._verify_device``'s gentlest-first short-circuiting (a frozen heartbeat
        or a failed snapshot skips every heavier, more perturbing check below it — the traffic
        pass is only safe to run once nothing is known to be frozen), but records every probe
        that DID run as an :class:`Observation` instead of collapsing straight to a bool. Each
        probe's existing ``(Optional[bool], detail)`` tri-state maps onto an Observation the same
        way: True -> HEALTHY, False -> UNHEALTHY, None -> SKIPPED.

        ``indices``/``expected`` let a caller that already read the mesh this pass (the gate reads
        both before it ever reaches a probe) hand them in instead of this method re-reading
        ``present_chip_indices()`` and re-deriving ``expected()`` itself — the latter WRITES the
        chip-baseline high-water mark, so a second, independent read/ratchet mid-pass can disagree
        with the caller's own. Omit both only where no such read exists yet (e.g. a bare test).

        ``log``, when given, gets the same per-probe line ``Recovery._verify_device`` used to
        write to the job log, and each probe's own ``set_device_op_detail`` string — both are what
        ``Recovery._verify_device`` now delegates here for (see below); dropping them here would
        silently blank the job log and the queue's "what's it doing" detail for every caller.
        """
        if expected is None:
            if indices is None:
                indices = await asyncio.to_thread(self._present_chip_indices)
            expected = self.expected(len(indices))
        observations: list[Observation] = []

        def _record(monitor: str, ok: Optional[bool], detail: str, evidence: Optional[dict] = None) -> None:
            verdict = Verdict.HEALTHY if ok is True else Verdict.UNHEALTHY if ok is False else Verdict.SKIPPED
            observations.append(
                Observation(monitor=monitor, verdict=verdict, detail=detail, evidence=evidence or {}, phase=phase)
            )

        def _store() -> HealthState:
            self._readings = HealthState(
                phase=phase, at=datetime.now(), expected=expected, observations=tuple(observations)
            )
            return self._readings

        # Ahead of the heartbeat, and ahead of the snapshot, because it is the only probe that
        # needs nothing to have worked yet: pure sysfs bus reads, no driver attribute, no device
        # open, no UMD. A chip the driver never bound has no /sys/class/tenstorrent entry at all,
        # so the heartbeat below is simply absent for it and the tt-smi snapshot fails for a
        # reason it cannot name — this says which chip and why. UNHEALTHY rather than evidence
        # because both faults it judges make the device unusable AND are the kind the gentlest
        # rung actually repairs: a PCI rescan re-binds a driver that failed to attach, and an
        # unassigned BAR after hotplug is exactly what a rescan places.
        self._deps.set_device_op_detail("health check: host PCI (driver binding, BARs)")
        _t0 = time.monotonic()
        hp_ok, hp_detail, hp_evidence = await asyncio.to_thread(hostpci.host_pci_verdict)
        metrics.probe_observed("hostpci", _verdict_label(hp_ok), time.monotonic() - _t0)
        if log:
            log(f"host-pci: {'SKIPPED' if hp_ok is None else ('OK' if hp_ok else 'UNHEALTHY')} — {hp_detail}")
        _record("hostpci", hp_ok, hp_detail, hp_evidence)
        if hp_ok is False:
            return _store()

        # Skipped only where the driver never exposed the attribute — not as a way to excuse a
        # device that failed the probe. See health.monitors.heartbeat.heartbeat_supported(). Routed
        # through deps, not the module-level function, so a test that monkeypatches
        # srv.heartbeat_supported()/heartbeat_verdict (the seam Recovery._verify_device has always
        # used) still controls this probe now that this is the one place it runs.
        if self._deps.heartbeat_supported():
            self._deps.set_device_op_detail("health check: chip heartbeat")
            hb, hb_detail, hb_evidence = await asyncio.to_thread(self._deps.heartbeat_verdict, expected)
            if log:
                log(f"heartbeat: {hb.value.upper()} — {hb_detail}")
            observations.append(
                Observation(monitor="heartbeat", verdict=hb, detail=hb_detail, evidence=hb_evidence, phase=phase)
            )
            if hb is Verdict.UNHEALTHY:
                return _store()

        self._deps.set_device_op_detail("health check: chip enumeration (tt-smi)")
        ok, detail = await self._verify_device(expected)
        if log:
            log(f"snapshot: {'OK' if ok else 'UNHEALTHY'} — {detail}")
        _record("pci", ok, detail)
        fabric_asked = run_fabric or force_fabric
        if ok and (fabric_asked or run_eth):
            self._deps.set_device_op_detail("health check: eth-core heartbeat (passive)")
            if fabric_asked:
                eok, edetail = await self.verify_eth_heartbeat()
            else:
                eok, edetail = await self.verify_eth_heartbeat(timeout_sec=ETH_POST_JOB_TIMEOUT_SEC)
            if log:
                log(f"eth-heartbeat: {'SKIPPED' if eok is None else ('OK' if eok else 'FROZEN')} — {edetail}")
            _record("eth_heartbeat", eok, edetail)
            if not fabric_asked and eok is None and log:
                log("eth-heartbeat reached no verdict after a clean exit — running the fabric traffic pass")
            if eok is not False and (fabric_asked or eok is None):
                self._deps.set_device_op_detail("health check: fabric traffic pass across all links (~45s)")
                fok, fdetail = await self.verify_fabric_health()
                if log:
                    log(f"fabric: {'SKIPPED' if fok is None else ('OK' if fok else 'UNHEALTHY')} — {fdetail}")
                _record("fabric", fok, fdetail)

        return _store()

    async def _verify_device(self, expected: int) -> tuple[bool, str]:
        """The tt-smi snapshot probe, off the event loop. A thin seam ``update()`` calls through,
        separated out so a test can replace the whole probe without shelling out to tt-smi."""
        return await asyncio.to_thread(self.verify_device_health, expected)

    def expected(self, present: int) -> int:
        """How many chips this host SHOULD have — never the survivor count.

        Every check took ``expected`` from the /dev nodes that were present, so a mesh that lost a
        tray silently lowered its own bar: a 24-chip box verified 24-of-24 and reported HEALTHY,
        while a job needing the full mesh could not build one. The device was fine about whoever
        was left. The truth comes from an explicit per-host override, else a high-water mark of
        what this host has actually shown — chips disappear from a wedge, not from the design, so
        the bar only ever ratchets up.
        """
        env = os.environ.get("TT_DEVICE_MCP_EXPECTED_CHIPS", "").strip()
        if env.isdigit() and int(env) > 0:
            return int(env)
        path = health_dir() / self.CHIP_BASELINE_FILE
        try:
            baseline = int(json.loads(path.read_text()).get("chips", 0))
        except FileNotFoundError:
            # No baseline was ever written: this host has never shown a chip, so it is
            # device-less until one enumerates. present (0 on the all-off-bus path) is the
            # honest floor, and the gate's empty-/dev branch is allowed to release on it.
            baseline = 0
        except (OSError, ValueError, TypeError, AttributeError):
            # The baseline file EXISTS but is unreadable or corrupt. Its existence is proof
            # this host has shown chips, so it is never a device-less box — collapsing it to 0
            # is the F1 inversion: with present==0 that returns 0, and the gate reads a baseline
            # it merely could not read as "expects no chips" and releases the hold on a mesh that
            # dropped every chip. Absence of a readable count is not absence of chips; fail closed
            # to a nonzero expectation so the empty-/dev branch stays held and escalates.
            return max(present, 1)
        if present > baseline:
            try:
                path.write_text(json.dumps({"chips": present}))
            except OSError:
                pass  # a baseline we cannot persist must not break the gate; present is the floor
            return present
        return baseline or present

    def eth_link_drop(self, measured: int) -> str:
        """Judge the built-in eth probe's measured link count against this host's high-water mark.

        The probe reads only links that are up, so a link that went down is simply one core fewer
        and every remaining core still reads advancing. The count is the only place that loss
        shows. Returns "" when ``measured`` is at least the mark (and ratchets the mark up), else
        the reason the read cannot vouch for the fabric. Like the chip baseline, the mark only
        rises: links go down from a wedge, not from the design. An operator re-baselines a host
        whose links really changed by deleting the file. An unreadable file fails closed for this
        read and is rewritten with the current count, so a file lost that way loses its mark.
        Writes go through a temp file and ``os.replace``, so a crash mid-write cannot tear it.
        """
        path = health_dir() / self.ETH_LINK_BASELINE_FILE
        try:
            baseline = int(json.loads(path.read_text()).get("links", 0))
            corrupt = False
        except FileNotFoundError:
            baseline, corrupt = 0, False
        except (OSError, ValueError, TypeError, AttributeError):
            baseline, corrupt = 0, True
        if measured > baseline or corrupt:
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text(json.dumps({"links": measured}))
                os.replace(tmp, path)
            except OSError:
                pass  # a mark we cannot persist must not break the gate
        if corrupt:
            return f"eth link baseline unreadable; re-baselined at {measured} up link(s)"
        if measured < baseline:
            return f"{measured} of {baseline} eth link(s) up — a link went down since the high-water mark"
        return ""

    def _health_check_enabled(self) -> bool:
        """Whether the tt-smi snapshot health check runs around jobs. On by default;
        set TT_DEVICE_MCP_HEALTH_CHECK=0 to disable (e.g. a host without tt-smi)."""
        return os.environ.get("TT_DEVICE_MCP_HEALTH_CHECK", "1").strip() != "0"

    def _fabric_check_cmd(self) -> str:
        """The configured operator override for the fabric traffic check (e.g. a custom wrapper
        around run_cluster_validation, or deploy/tt-device-fabric-check.sh). Empty => no
        override; verify_fabric_health falls to the built-in validator path instead (see
        health.monitors.fabric.build_command). Set via TT_DEVICE_MCP_FABRIC_CHECK_CMD. The
        command must exit 0 when the fabric is healthy, 77 when it could not check at all (no
        verdict either way — ignored, never a reset trigger), and non-zero only when the fabric
        is genuinely unhealthy. Its stdout is never interpreted; only its exit code is."""
        return os.environ.get("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "").strip()

    def _eth_heartbeat_cmd(self) -> str:
        """The operator's own non-perturbing eth-core heartbeat check. Empty => not overridden;
        ``verify_eth_heartbeat`` falls to the built-in probe instead (see ``health.monitors.eth``).

        Set via TT_DEVICE_MCP_ETH_HEARTBEAT_CMD. It reads each active-ethernet-core's firmware
        heartbeat register (a passive MMIO read, no traffic pushed) and exits 0 when every core's
        heartbeat is advancing, non-zero when one is frozen, 77 when it could not check. A frozen
        core is the wedge itself; catching it by reading — not by pushing traffic across it, which
        is what knocks the frozen chip off the PCIe bus — is the whole point of running this first."""
        return os.environ.get("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "").strip()

    def _journal_skip_once(self, kind: str, reason: str, **fields) -> None:
        """Record, at most once per process, that a health check produced NO verdict.

        A no-verdict skip means the mesh went unvalidated on that axis, which doctrine forbids passing
        silently. But these skips are a fixed condition seen on every gate, so the first is the whole
        signal — deduping on (kind, reason) keeps the loud record from becoming the noise an operator
        learns to scroll past."""
        key = (kind, reason)
        if key in self._skip_events_journaled:
            return
        self._skip_events_journaled.add(key)
        health_event(kind, reason=reason, **fields)

    def _bank_board_types_once(self, types: list) -> None:
        """Record the board types seen in a tt-smi snapshot. The boards cannot change under a running
        broker, so the first identified read stands; a later short or empty one is not allowed to erase
        it, because losing the machine type is what silently downgrades a Galaxy's reset.

        A snapshot tt-smi could not identify is not a read of the machine type. Caching it would pin
        this process to "unknown" for its whole life and lock out the identified snapshot that follows a
        recovery — so it is left unset instead, and the next snapshot gets to fill it.
        """
        if self._board_types is not None or not types:
            return
        named = [t for t in types if _normalized_board(t) not in _UNIDENTIFIED_BOARDS]
        if len(named) != len(types):
            self._journal_skip_once(
                "board_types_unidentified",
                f"tt-smi identified {len(named)}/{len(types)} boards — machine type "
                f"stays unknown until a snapshot identifies all of them",
            )
            return
        self._board_types = list(types)

    def _bank_bus_ids_once(self, bus_ids: list, expected_count: int) -> None:
        """Record every chip's PCI bus id from a tt-smi snapshot, first complete read standing.

        Complete or not at all: a snapshot taken while chips were already off the bus is short, and
        a short list read positionally would attribute one chip's bus to another's id. The trays
        derived from that (I16) would name healthy silicon, so a partial read is discarded and the
        next full snapshot gets to fill it. Both halves of "complete" are enforced here rather than
        left to the caller: ``expected_count == 0`` (the boot platform probe reads only board types
        and does not know the count) and any snapshot shorter than the mesh are both refused, and
        so is a chip tt-smi enumerated without a bus id.
        """
        if self._bus_ids is not None:
            return
        if expected_count <= 0 or len(bus_ids) < expected_count:
            return
        if not bus_ids or not all(bus_ids):
            return
        self._bus_ids = list(bus_ids)

    def _bus_id_cache_expected_count(self, expected_count: int) -> int:
        """The smallest snapshot size that is allowed to freeze ``_bus_ids``.

        The raw ``expected_count`` is not always enough. On a fresh broker a degraded first pass can
        seed the chip baseline from its survivor count; caching a Galaxy's bus ids against that lower
        bar would permanently bank a partial tray map until restart. The board type therefore
        tightens the count when it can: a known Galaxy needs all 32 chips, and an unidentified
        machine type means "no map yet" rather than freezing a positional guess for a later
        identified snapshot to inherit.
        """
        board_types = {_normalized_board(t) for t in (self._board_types or ())}
        if len(board_types) != 1:
            return 0
        if board_types.pop() in self._glx_board_types():
            return max(expected_count, 32)
        return expected_count

    def _glx_board_types(self) -> tuple:
        """tt-smi's own Galaxy board types — the single source of truth, never copied here. Empty when
        tt-smi cannot be imported, which leaves the machine type unknown rather than guessed.

        Imported here, not at module scope: `tt_smi` pulls in a native extension and a TUI stack to
        reach a two-element list, and importability differs from tt-smi being on PATH.
        """
        if self._glx_cache is None:
            try:
                from tt_smi.constants import GLX_BOARD_TYPES

                self._glx_cache = tuple(GLX_BOARD_TYPES)
            except Exception as e:  # noqa: BLE001 - a native extension failing here must not kill boot
                self._journal_skip_once("glx_types", f"tt-smi board-type list unavailable: {e}")
                self._glx_cache = ()
        return self._glx_cache

    def verify_device_health(self, expected_count: int, *, timeout_sec: float = 90.0) -> tuple[bool, str]:
        """Verify the mesh is actually usable after a reset, via a read-only tt-smi
        snapshot. A reset exiting 0 only means the command ran — it does NOT prove the
        chips came back. This is the check that turns "reset ran" into "device healthy".

        Returns ``(ok, detail)``. ``ok`` is True only when ALL of:
          * the snapshot itself succeeds (ARC mailbox answered within ``timeout_sec``),
          * at least ``expected_count`` chips enumerate (none dropped off the mesh — the
            UBB-tray-down signature is a short count here), and
          * every chip reports a ``board_id`` and non-empty telemetry (its ARC is alive,
            not wedged returning sentinels).

        Scope: this proves chips enumerate + ARC is responsive. True inter-chip fabric
        (ethernet) liveness only manifests under a collective, so a green result means
        "all chips present and answering", which is the strongest signal obtainable
        without running a CCL workload in the between-jobs gate. Never raises — any
        failure is returned as ``(False, reason)`` so callers stay non-blocking.

        Note: ``tt-smi -s`` dumps the snapshot JSON to STDOUT and exits before any
        ``-f`` file is written, so we parse stdout (``--snapshot_no_tty`` forces the
        machine-readable JSON form even when attached to a tty).

        Wrapped by a thin timer that reports the "pci" probe metric (see
        :func:`_verify_device_health_body`) — every caller of this method (the gate,
        ``Recovery._verify_device``, and ``HealthMonitor.update()``) is covered from this one
        instrumentation point.
        """
        _t0 = time.monotonic()
        ok, detail = self._verify_device_health_body(expected_count, timeout_sec=timeout_sec)
        metrics.probe_observed("pci", _verdict_label(ok), time.monotonic() - _t0)
        return ok, detail

    def _verify_device_health_body(self, expected_count: int, *, timeout_sec: float = 90.0) -> tuple[bool, str]:
        try:
            try:
                proc = subprocess.run(
                    ["tt-smi", "-s", "--snapshot_no_tty"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                return False, f"tt-smi snapshot timed out after {timeout_sec:.0f}s (ARC unresponsive)"
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[-200:]
                return False, f"tt-smi snapshot exited {proc.returncode}: {tail}"
            try:
                snap = json.loads(proc.stdout)
            except ValueError as e:
                tail = (proc.stderr or "").strip().replace("\n", " ")[-200:]
                return False, f"could not parse tt-smi snapshot JSON: {e}" + (f" (stderr: {tail})" if tail else "")
        except Exception as e:  # noqa: BLE001 - health check must never raise into the gate
            return False, f"snapshot error: {e}"

        devs = snap.get("device_info") or []
        # The board types ride along: this snapshot is the only tt-smi call the broker makes, and the
        # reset argv needs the machine type. Taking it here means no second device init — least of all
        # one aimed at silicon this check has just declared unhealthy.
        self._bank_board_types_once([(d.get("board_info") or {}).get("board_type") or "" for d in devs])
        # A short count is a drop; an over-count is not. ``expected_count`` can be a stale
        # high-water mark baked while the mesh was degraded, so a reset that brings back MORE
        # chips than that bar is a recovery — scoring it a failure would escalate a healthy box.
        if len(devs) < expected_count:
            return False, (
                f"{len(devs)} chip(s) enumerated, expected {expected_count} "
                f"(chips dropped off the mesh — likely a tray down)"
            )
        # The count guard lives inside _bank_bus_ids_once, but the count itself has to be tightened
        # here: a boot platform probe passes expected_count=0, and a fresh degraded first pass can
        # hand us the survivor count before the baseline ever saw the whole mesh. Neither is allowed
        # to freeze a positional bus map (I16).
        fresh_bus_ids = [(d.get("board_info") or {}).get("bus_id") or "" for d in devs]
        bus_id_expected_count = self._bus_id_cache_expected_count(expected_count)
        self._bank_bus_ids_once(fresh_bus_ids, bus_id_expected_count)
        # Topology-drift observability: the cache is one-shot for the process, but the very rung
        # I16 enables re-powers trays, and the driver's bus enumeration after a warm reset is not
        # guaranteed to match boot. Journal a mismatch so a drift is visible; the cached map
        # stands, since accepting a fresh one would open a race where a bad snapshot replaces a
        # known-good one. Only compares full-count reads — anything shorter is an already-degraded
        # mesh whose absent chips look like drift.
        if (
            self._bus_ids is not None
            and bus_id_expected_count > 0
            and len(fresh_bus_ids) >= bus_id_expected_count
            and all(fresh_bus_ids)
            and list(fresh_bus_ids) != self._bus_ids
        ):
            self._journal_skip_once(
                "bus_map_drift",
                "cached per-chip bus map differs from the current snapshot — a warm reset may "
                "have re-enumerated chips (I16); the cached map stands",
            )
        silent = []
        for i, d in enumerate(devs):
            binfo = d.get("board_info") or {}
            telem = d.get("telemetry") or {}
            if not binfo.get("board_id") or not telem:
                silent.append(str(i))
        if silent:
            return False, f"chip(s) [{','.join(silent)}] returned no board_id/telemetry (ARC wedged)"
        return True, f"all {len(devs)} chips enumerated with live ARC telemetry"

    async def verify_fabric_health(self, timeout_sec: float = FABRIC_CHECK_TIMEOUT_SEC) -> tuple[Optional[bool], str]:
        """Run the fabric traffic check (see ``health.monitors.fabric``), which pushes packets
        across every inter-chip ethernet link and reports non-zero if any link is unhealthy.
        This is the only check that proves the FABRIC actually moves data — the tt-smi snapshot
        only proves chips enumerate + ARC is alive.

        Returns ``(ok, detail)``:
          * ``None`` => SKIPPED (not configured, no built-in validator installed, the command
            isn't runnable, or the check ran but reached no verdict) — the caller must NOT
            reset on a skip.
          * ``True``  => fabric healthy.
          * ``False`` => fabric unhealthy, including a broker-side timeout — the caller should
            reset.
        Never raises.

        Wrapped by a thin timer that reports the "fabric" probe metric — the pass whose duration
        is the one worth alerting on, since it is the closest to its own timeout of the four."""
        _t0 = time.monotonic()
        ok, detail = await self._verify_fabric_health_body(timeout_sec)
        metrics.probe_observed("fabric", _verdict_label(ok), time.monotonic() - _t0)
        return ok, detail

    async def _verify_fabric_health_body(self, timeout_sec: float) -> tuple[Optional[bool], str]:
        override = self._fabric_check_cmd()
        built = fabric.build_command()
        if built is None:
            # The authoritative fabric verdict, and preflight hard-requires it — so reaching here means
            # the mesh goes entirely unvalidated on the one check that proves it moves data. Journal it.
            self._journal_skip_once("fabric_check_unavailable", "not_configured")
            return None, "skipped (TT_DEVICE_MCP_FABRIC_CHECK_CMD not set, and no built-in validator installed)"
        argv, env = built
        cmd = override or " ".join(argv)
        # The runtime-root cwd is a built-in-path concern only: run_cluster_validation resolves
        # its kernels from it, but an operator's override runs wherever the broker itself runs,
        # exactly as before this module existed — env for that path is just os.environ, and
        # deriving cwd from whatever TT_METAL_RUNTIME_ROOT happens to be ambient there would
        # silently relocate the override's cwd too.
        cwd = None if override else env.get("TT_METAL_RUNTIME_ROOT")
        _t0 = datetime.now()
        # Open the jobs-list row now, not on completion: the traffic pass holds the device for
        # ~45-100s, and a row written only at the end leaves that window a blank in the audit
        # trail with no id an operator can follow.
        self._begin_action_row("[broker]fabric-check", cmd)

        def _track(proc: Optional[asyncio.subprocess.Process]) -> None:
            # Tracked so a dead chip can kill it (see server._kill_device_holders). This process
            # maps every chip's BARs, so when one leaves the bus it is the likeliest thing still
            # reading a dead address — and removing the endpoint does not revoke its mapping. It
            # has killed a host twice.
            self.fabric_check_proc = proc

        try:
            rc, text = await fabric.check(
                argv, env, timeout_sec=timeout_sec, track=_track, terminate=self._terminate_process_group, cwd=cwd
            )
        except Exception as e:  # noqa: BLE001 - configured but could not spawn; never raise into the gate
            self._journal_skip_once("fabric_check_unavailable", "not_runnable", detail=str(e)[:400])
            return None, f"skipped (fabric check not runnable: {e})"

        dt = (datetime.now() - _t0).total_seconds()
        # Keep the whole thing: the validator names the failing links, and the 400-char detail
        # the journal carries truncates exactly the part naming them.
        self.last_fabric_output = text
        last = text.splitlines()[-1][:200] if text else "(no output)"
        if override:
            # An operator's own wrapper has already done its own interpretation and reports it
            # purely through its exit code — its stdout is never run through classify(), which
            # means something only for the built-in validator's own output signatures.
            ok = True if rc == 0 else None if rc == FABRIC_CHECK_CANNOT_CHECK_RC else False
            reason = last
        else:
            ok, reason = fabric.classify(rc, text)
        # Derived from ok, not rc: on the built-in path a measured-bad verdict can exit 77 (the
        # collision guard classify() applies), and a did-not-measure run can exit some other
        # code entirely — status must agree with the verdict actually being returned below, not
        # with the raw exit code that verdict may have been remapped away from. A broker-side
        # timeout (rc is None) is its own thing: a live run that ran out of time, not a skip.
        status = "timeout" if rc is None else "completed" if ok is True else "skipped" if ok is None else "failed"
        self._write_action_log("[broker]fabric-check", cmd, dt, status, rc)

        if ok is True:
            self.last_fabric_ok = True
            return True, f"fabric links healthy ({dt:.0f}s)"
        if ok is None:
            # The check could not run — it learned nothing about the fabric, so resetting
            # on it would be resetting on our own missing install. Loud, because a host
            # in this state has no fabric coverage at all and that must not pass silently.
            log = self._logger()
            if log:
                log.warning(f"FABRIC-CHECK not runnable on this host — no fabric coverage: {reason[:200]}")
            health_event("fabric_check_unavailable", detail=reason[:400], cmd=cmd)
            return None, f"fabric check COULD NOT RUN ({dt:.0f}s): {reason[:200]}"
        self.last_fabric_ok = False
        if rc is None:
            return False, f"fabric check timed out after {timeout_sec:.0f}s; last line: {last}"
        return False, f"fabric check exited {rc} ({dt:.0f}s): {reason[:200]}"

    async def verify_eth_heartbeat(self, timeout_sec: float = 60.0) -> tuple[Optional[bool], str]:
        """Read the active-ethernet-core firmware heartbeats — a passive check that never pushes
        traffic — and say whether any core is frozen. Same override/built-in split as
        ``verify_fabric_health`` (see ``health.monitors.eth``): ``TT_DEVICE_MCP_ETH_HEARTBEAT_CMD``
        set names an operator's own command, judged on exit code alone; unset, this runs the
        built-in probe. Both self-block (see ``eth.build``) until the startup self-test has timed
        the read on this box and armed the rung — a disarmed reader skips, never holds.

        Returns ``(ok, detail)`` on the same contract as the fabric check:
          * ``None``  => SKIPPED (not configured/available, exit 77, or could not spawn) — say
            nothing, and fall through to the traffic pass.
          * ``True``  => every active-eth-core heartbeat is advancing.
          * ``False`` => a core's heartbeat is frozen, OR the read itself hung past ``timeout_sec``.
            A frozen core can hang the read, so THIS timeout is evidence of the wedge, not a skip —
            distinct from the built-in probe's OWN, tighter bound (``eth.probe_timeout_sec``, kept
            below ``timeout_sec`` on purpose), whose expiry is "the read did not finish in its own
            budget" and folds to a skip via ``classify_exit(None)``, mirroring the old wrapper's
            124-under-``timeout(1)`` case. The caller must route a ``False`` to HOLD and must NOT
            run the traffic pass, which would push the frozen chip off the PCIe bus. This matches
            verify_fabric_health, whose own timeout is also False.
        Never raises.

        Wrapped by a thin timer that reports the "eth_heartbeat" probe metric — the label
        HealthMonitor.update() already uses for this Observation (see ``_record`` calls below)."""
        _t0 = time.monotonic()
        ok, detail = await self._verify_eth_heartbeat_body(timeout_sec)
        metrics.probe_observed("eth_heartbeat", _verdict_label(ok), time.monotonic() - _t0)
        return ok, detail

    async def _verify_eth_heartbeat_body(self, timeout_sec: float) -> tuple[Optional[bool], str]:
        override = self._eth_heartbeat_cmd()
        # build() can shell out up to three candidate pythons (health.monitors.eth.resolve_python)
        # once armed — off the event loop, or a health gate stalls the job queue, MCP requests,
        # and the sd_notify watchdog ping for as long as those subprocess.run calls take.
        built = await asyncio.to_thread(eth.build)
        if built is None:
            # Nothing to check with: the rung is disarmed (override or built-in alike — the
            # startup self-test's disarm must keep an untimed reader from delivering a HOLD),
            # or no reader is wired (no python imports ttexalens, or the probe is missing). All
            # collapse to the same "learned nothing" signal, but NOT the same journal key:
            # unavailable() (off-loop — it re-resolves) keys the skip not_armed vs not_configured
            # with the actionable cause in the detail, so a host whose cause crosses that boundary
            # over the process lifetime journals both sides.
            key, reason = await asyncio.to_thread(eth.unavailable)
            self._journal_skip_once("eth_heartbeat_unavailable", key, detail=reason[:400])
            return None, f"skipped (no runnable eth-heartbeat read: {reason})"
        argv, env = built
        # The runtime-root cwd is a built-in-path concern only, mirroring verify_fabric_health:
        # ttexalens resolves relative paths off the tree its python was built from, but an
        # operator's override runs wherever the broker itself runs, exactly as before this module
        # existed.
        cwd = None if override else env.get("TT_METAL_RUNTIME_ROOT")
        # The override has no separate probe-timeout knob (never did): it uses the caller's own
        # bound outright. The built-in path's own knob (eth.probe_timeout_sec) is an operator
        # value from /etc/default, not something this code can trust to stay below timeout_sec on
        # its own — enforced here so a misconfigured TTDEV_ETH_CHECK_TIMEOUT can never collapse the
        # two distinct timeout outcomes (a merely-slow read skipping vs. the caller's own hang
        # evidence) into one.
        probe_timeout = timeout_sec if override else min(eth.probe_timeout_sec(), timeout_sec * 0.9)

        # Survives track(None): run_probe's own `finally` clears fabric_check_proc on every exit
        # path, INCLUDING when the outer wait_for below cancels this call — a CancelledError
        # unwinds through run_probe without ever reaching its own `except TimeoutError`, so
        # run_probe never kills the group in that case. Without an independent holder, the except
        # block below would have nothing left to killpg, and the reader would outlive a HOLD
        # verdict, still attached to the wedged mesh.
        held: dict[str, Optional[asyncio.subprocess.Process]] = {"proc": None}

        def _track(proc: Optional[asyncio.subprocess.Process]) -> None:
            # Tracked so a dead chip can kill it (see server._kill_device_holders). It touches
            # chip memory, so a chip leaving the bus mid-read is the likeliest thing still reading
            # a dead address.
            self.fabric_check_proc = proc
            if proc is not None:
                held["proc"] = proc

        _t0 = datetime.now()
        try:
            rc, text = await asyncio.wait_for(
                eth.check(argv, env, timeout_sec=probe_timeout, track=_track, cwd=cwd),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            # eth.check() bounds itself to probe_timeout (< timeout_sec for the built-in path)
            # and always returns rather than raising; reaching HERE means even that bound plus its
            # own killpg cleanup did not complete inside timeout_sec, which is itself frozen-core
            # evidence, not a skip. The process itself is still alive at this point (see `held`
            # above) — kill it now, or a HOLD verdict leaves a reader attached to a wedged mesh.
            proc = held["proc"]
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            return False, f"eth-heartbeat read hung past {timeout_sec:.0f}s (a frozen core can hang the read)"
        except Exception as e:  # noqa: BLE001 - never raise into the gate
            # Wired but could not spawn — a configured rung silently producing no verdict is the
            # degrade worth a loud record, unlike the sanctioned unavailable default above.
            self._journal_skip_once("eth_heartbeat_unavailable", "not_runnable", detail=str(e)[:400])
            return None, f"skipped (eth-heartbeat read not runnable: {e})"

        dt = (datetime.now() - _t0).total_seconds()
        last = text.splitlines()[-1][:200] if text else "(no output)"
        if override:
            # An operator's own command has already done its own interpretation and reports it
            # purely through its exit code — its stdout is never run through classify_exit(),
            # which means something only for the built-in probe's own sentinel (3).
            ok = True if rc == 0 else None if rc == FABRIC_CHECK_CANNOT_CHECK_RC else False
            reason = last
        else:
            ok, reason = eth.classify_exit(rc)
            links = eth.parse_link_count(text)
            # A down link is invisible to the verdict above (the probe skips it), so a drop in the
            # measured count turns an all-advancing read into "could not vouch": the caller then
            # runs the traffic pass, which is what tests every link. A frozen verdict stays frozen.
            drop = self.eth_link_drop(links) if links else ""
            if ok is True and drop:
                self._journal_skip_once("eth_heartbeat_unavailable", "link_count_drop", detail=drop[:400])
                return None, f"skipped (eth links unverified): {drop}"

        if ok is None:
            # The check could not run — it learned nothing about the eth cores, so falling
            # through to the traffic pass is correct, but a configured rung that got no verdict
            # deserves a loud record, mirroring the fabric check's own rc-77 path.
            self._journal_skip_once("eth_heartbeat_unavailable", "could_not_check", detail=last[:400])
            return None, f"skipped (eth-heartbeat read could not check): {reason}"
        if ok is False:
            return False, f"a frozen active-eth core ({dt:.0f}s): {reason}"
        return True, f"all active-eth cores advancing ({dt:.0f}s)"
