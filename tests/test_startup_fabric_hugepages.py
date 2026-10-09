# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The startup fabric pass waits for the 1G hugepage pool, and a hold whose only cause is a fabric
77 (could not check) is re-checked read-only, never escalated to a galaxy reset (spec 03 I33, I34).

blx01, 2026-10-08: the broker started before tenstorrent-hugepages.service had allocated the pool,
the startup fabric pass exited 77 with its reason cut to "no link was tested", and the idle relift
then escalated that hold to a glx_reset 120 s later — a reset that cannot produce a fabric verdict.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import monitor as health_monitor_mod
from tt_device_mcp.health.monitors import hostpci, pci

# --- hostpci.hugepages_shortfall ---------------------------------------------------------------


def _fake_bus(tmp_path, monkeypatch, *, iommu_type, nr="4", n_chips=4):
    """A PCI tree of ``n_chips`` Tenstorrent functions behind one IOMMU group of ``iommu_type``
    (None: no IOMMU group link at all), and a 1G pool holding ``nr`` pages (None: no pool)."""
    devices = tmp_path / "devices"
    devices.mkdir()
    group = tmp_path / "iommu_groups" / "7"
    group.mkdir(parents=True)
    if iommu_type is not None:
        (group / "type").write_text(iommu_type + "\n")
    for i in range(n_chips):
        dev = devices / f"0000:{i + 1:02x}:00.0"
        dev.mkdir()
        (dev / "vendor").write_text(hostpci.TT_VENDOR_ID + "\n")
        if iommu_type is not None:
            (dev / "iommu_group").symlink_to(group)
    monkeypatch.setattr(pci, "PCI_DEVICES_DIR", devices)
    pool = tmp_path / "nr_hugepages"
    if nr is not None:
        pool.write_text(nr + "\n")
    monkeypatch.setattr(hostpci, "HUGEPAGES_1G_NR", pool)


@pytest.mark.parametrize("iommu_type", ["identity", None])
def test_hugepages_shortfall_reports_a_short_pool_behind_identity_or_no_iommu(tmp_path, monkeypatch, iommu_type):
    _fake_bus(tmp_path, monkeypatch, iommu_type=iommu_type, nr="3", n_chips=32)
    assert hostpci.hugepages_shortfall() == (3, 32)


def test_hugepages_shortfall_is_none_once_the_count_is_met(tmp_path, monkeypatch):
    _fake_bus(tmp_path, monkeypatch, iommu_type="identity", nr="32", n_chips=32)
    assert hostpci.hugepages_shortfall() is None


def test_hugepages_shortfall_is_none_behind_a_translating_iommu(tmp_path, monkeypatch):
    """A DMA domain maps buffers through the IOMMU and needs no hugepages: nothing to wait for."""
    _fake_bus(tmp_path, monkeypatch, iommu_type="DMA-FQ", nr="0")
    assert hostpci.hugepages_shortfall() is None


def test_hugepages_shortfall_is_none_without_a_readable_pool(tmp_path, monkeypatch):
    """No 1G pool to read is not evidence of a short one; waiting on it would hold a box that may
    not use hugepages at all."""
    _fake_bus(tmp_path, monkeypatch, iommu_type="identity", nr=None)
    assert hostpci.hugepages_shortfall() is None


def test_hugepages_shortfall_is_none_with_no_function_on_the_bus(tmp_path, monkeypatch):
    _fake_bus(tmp_path, monkeypatch, iommu_type="identity", nr="0", n_chips=0)
    assert hostpci.hugepages_shortfall() is None


def test_hugepages_shortfall_counts_pci_functions_not_chips(tmp_path, monkeypatch):
    """UMD pins one 1G page per PCI function, and the vendor setup allocates per function. A card
    with two chips behind one function (n300, T3K) needs one page, not two: keyed on the chip
    count, a pool sized per function never reads full, and the startup pass and the relift wait
    on it forever."""
    _fake_bus(tmp_path, monkeypatch, iommu_type="identity", nr="4", n_chips=4)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "8")
    assert hostpci.hugepages_shortfall() is None
    assert srv._hugepages_shortfall() is None, "8 chips on 4 functions need 4 pages, and 4 are there"


def test_hugepages_shortfall_needs_a_page_only_per_identity_mapped_function(tmp_path, monkeypatch):
    """A function behind a translating domain needs no page; only the identity-mapped ones count."""
    _fake_bus(tmp_path, monkeypatch, iommu_type="identity", nr="1", n_chips=2)
    dma = tmp_path / "iommu_groups" / "8"
    dma.mkdir()
    (dma / "type").write_text("DMA\n")
    for i in (3, 4):
        dev = pci.PCI_DEVICES_DIR / f"0000:{i:02x}:00.0"
        dev.mkdir()
        (dev / "vendor").write_text(hostpci.TT_VENDOR_ID + "\n")
        (dev / "iommu_group").symlink_to(dma)
    assert hostpci.hugepages_shortfall() == (1, 2)
    (tmp_path / "nr_hugepages").write_text("2\n")
    assert hostpci.hugepages_shortfall() is None


def test_the_broker_waits_only_when_the_expected_chip_count_is_set(monkeypatch):
    seen = []
    monkeypatch.setattr(srv, "health_hugepages_shortfall", lambda: seen.append(1) or (1, 32))
    assert srv._hugepages_shortfall() is None, "no TT_DEVICE_MCP_EXPECTED_CHIPS: unchanged behaviour"
    assert seen == []
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "32")
    assert srv._hugepages_shortfall() == (1, 32)
    monkeypatch.setenv("TT_DEVICE_MCP_EXPECTED_CHIPS", "0")
    assert srv._hugepages_shortfall() is None, "a declared count of 0 declares nothing"


# --- the fabric wrapper's reason and the action-log output -------------------------------------


def test_override_reason_prefers_the_cause_over_the_terminate_banner():
    text = (
        "fabric-check: validating 32 chips\n"
        "terminate called after throwing an instance of 'std::runtime_error'\n"
        "  what():  Failed to allocate 32 hugepages\n"
        "fabric-check: no link was tested\n"
    )
    assert health_monitor_mod._override_reason(text) == "what():  Failed to allocate 32 hugepages"


def test_override_reason_finds_a_filesystem_error():
    text = "banner\nfilesystem error: cannot create directory: No space left on device\nlast line\n"
    assert health_monitor_mod._override_reason(text) == (
        "filesystem error: cannot create directory: No space left on device"
    )


def test_override_reason_falls_back_to_the_wrappers_final_line():
    """The wrapper prints its verdict, reason included, on the final line; blank lines after it
    must not hide it."""
    text = "fabric-check: banner\nfabric-check: validator did not complete (rc=1): last output: x\n\n"
    assert (
        health_monitor_mod._override_reason(text) == "fabric-check: validator did not complete (rc=1): last output: x"
    )
    assert health_monitor_mod._override_reason("") == "(no output)"


@pytest.mark.asyncio
async def test_a_skipped_fabric_run_keeps_its_output_in_the_action_log(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setenv(
        "TT_DEVICE_MCP_FABRIC_CHECK_CMD",
        "echo 'fabric-check: banner'; echo '  what():  hugepages short'; echo 'fabric-check: no link'; "
        f"exit {FABRIC_CHECK_CANNOT_CHECK_RC}",
    )

    ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=10)

    assert ok is None
    assert "what():  hugepages short" in detail, "the cause, not the last line, names the skip"
    logs = [p.read_text() for p in tmp_path.rglob("*") if p.is_file()]
    row = [t for t in logs if "[broker]fabric-check" in t or "fabric-check: banner" in t]
    assert row and "fabric-check: banner" in row[0], "the skipped run's output must be in its action log"
    assert "STATUS:      skipped" in row[0]


@pytest.mark.asyncio
async def test_a_healthy_fabric_run_keeps_its_action_log_short(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_CHECK_CMD", "printf 'chatty-%s\\n' validator-line; exit 0")

    ok, _ = await srv.health_monitor.verify_fabric_health(timeout_sec=10)

    assert ok is True
    logs = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert "chatty-validator-line" not in logs


# --- the startup hugepages wait ----------------------------------------------------------------


def _shortfalls(monkeypatch, seq):
    """``srv._hugepages_shortfall`` answers from ``seq``, then repeats its last answer."""
    it = iter(seq)
    last = {"v": None}

    def fake():
        last["v"] = next(it, last["v"])
        return last["v"]

    monkeypatch.setattr(srv, "_hugepages_shortfall", fake)
    monkeypatch.setattr(srv, "HUGEPAGES_POLL_SEC", 0.01)


@pytest.fixture
def _startup(monkeypatch, tmp_path, clear_job_state):
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "readopted_scopes", {})
    # Other suites leave chips isolated; a startup 77-only hold needs none.
    monkeypatch.setattr(srv, "isolated_chips", set())
    events = []
    patch_health_event(monkeypatch, lambda kind, **f: events.append((kind, f)))
    rows = []
    monkeypatch.setattr(srv, "write_action_log", lambda *a, **k: rows.append((a, k)))
    gate_calls = []

    async def fake_gate(job_log_file, *, phase, run_fabric, force_fabric=False):
        gate_calls.append(phase)

    monkeypatch.setattr(srv, "_device_health_gate", fake_gate)
    return events, rows, gate_calls


@pytest.mark.asyncio
async def test_startup_fabric_pass_waits_for_hugepages_then_runs(monkeypatch, _startup):
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [(0, 32), (16, 32), None])

    await srv._verify_fabric_on_start()

    assert gate_calls == ["startup"], "once the pool fills, the startup pass runs as before"
    assert rows == []


@pytest.mark.asyncio
async def test_startup_records_cannot_check_when_hugepages_stay_short(monkeypatch, _startup):
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [(5, 32)])
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.05)

    await srv._verify_fabric_on_start()

    assert gate_calls == [], "a pass run before the pool exists can only 77"
    assert srv.fsm.state is ServerState.RECOVERING
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "held as fabric-unverified, the 77-only hold"
    assert not srv.fsm.record.dirty, "a missing prerequisite owes no reset"
    assert ("fabric_check_unavailable", {"detail": "hugepages not yet allocated: 5/32", "cmd": "startup"}) in events
    ((args, kwargs),) = rows
    assert args[3] == "skipped" and args[4] == FABRIC_CHECK_CANNOT_CHECK_RC
    assert kwargs["output"] == "hugepages not yet allocated: 5/32"
    assert srv._fabric_77_only_hold()


@pytest.mark.asyncio
async def test_a_dirty_carried_episode_still_runs_the_startup_gate(monkeypatch, _startup):
    """A carried dirty mark is the gate's to reset+verify; the hugepages wait must not hide it."""
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [(5, 32)])
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.05)
    fsm_dirty(srv, "job 12 crashed", why="job_killed")

    await srv._verify_fabric_on_start()

    assert gate_calls == ["startup"]


# --- a 77-only hold: re-checked, never reset ---------------------------------------------------


def _hold_77(monkeypatch, tmp_path, *, last_fabric_ok=None):
    for i in range(32):
        (tmp_path / str(i)).write_text("")
    monkeypatch.setattr(srv, "TT_DEV_DIR", str(tmp_path))
    monkeypatch.setattr(srv.health_monitor, "expected", lambda present: 32)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", last_fabric_ok)
    monkeypatch.setenv("TT_DEVICE_MCP_DEVICE_OP_LOCK", "/proc/nonexistent/nope")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setattr(srv.recovery_mechanism, "scope_active", lambda: None)
    monkeypatch.setattr(srv, "device_op_lock", None)
    monkeypatch.setattr(srv, "device_op_active", "")
    monkeypatch.setattr(srv, "isolated_chips", set())
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: None)
    fsm_dirty(srv, "gate/startup: fabric unverified (could not run)", why="fabric_unverified")


def test_a_77_only_hold_arms_the_fabric_relift_not_the_generic_escalation(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)
    assert srv._idle_relift_armed() == (False, True, False)


def test_the_77_only_relift_has_an_off_switch(monkeypatch, tmp_path, clear_job_state):
    """Off, the hold just stands: still never the generic reset."""
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    assert srv._idle_relift_armed() == (False, False, False)


def test_a_fabric_hold_after_a_measured_fail_is_not_77_only(monkeypatch, tmp_path, clear_job_state):
    """A pass that measured the fabric BAD is a fault: the generic escalation applies again."""
    _hold_77(monkeypatch, tmp_path, last_fabric_ok=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)
    assert not srv._fabric_77_only_hold()
    assert srv._idle_relift_armed() == (False, False, True)


@pytest.mark.asyncio
async def test_the_77_only_relift_reruns_the_fabric_pass_and_never_resets(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)

    await srv._attempt_idle_relift()

    assert seen == [True], "the relift re-runs the read-only fabric pass"
    assert rungs == [], "a 77 is no fault: never a reset"
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY, "another 77 leaves the hold standing"


@pytest.mark.asyncio
async def test_the_77_only_relift_waits_for_hugepages(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_hugepages_shortfall", lambda: (8, 32))
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert seen == [], "a pass before the pool fills can only 77 again"
    assert srv.fsm.state is ServerState.RECOVERING


def _deadline(monkeypatch):
    fired = []
    monkeypatch.setattr(srv, "_maybe_spawn_forced_escalation", lambda: fired.append(True) or True)
    monkeypatch.setattr(srv, "device_hold_deadline_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_escalate_bucket", 0)
    monkeypatch.setattr(srv, "device_hold_offbus_escalated", False)
    monkeypatch.setattr(srv.recovery_mechanism, "cooling", lambda: False)
    srv.fsm.set_latch("escalated", False)
    since = datetime(2026, 10, 8, 3, 0, 0)
    monkeypatch.setattr(srv, "device_hold_episode_since", since.isoformat())
    monkeypatch.setattr(srv, "device_hold_episode_reason", "gate/startup: fabric unverified")
    return fired, since


def test_the_hold_deadline_never_forces_a_reset_on_a_77_only_hold(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    fired, since = _deadline(monkeypatch)
    srv._check_hold_deadline(now=since + timedelta(seconds=3 * srv._stuck_hold_ceiling_sec() + 1))
    assert fired == []


def test_the_hold_deadline_still_escalates_a_measured_fabric_fault(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path, last_fabric_ok=False)
    fired, since = _deadline(monkeypatch)
    srv._check_hold_deadline(now=since + timedelta(seconds=2 * srv._stuck_hold_ceiling_sec() + 1))
    assert fired, "the 77 exemption must not swallow a real fault"


@pytest.mark.asyncio
async def test_the_forced_escalation_bails_on_a_77_only_hold(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    climbed = []

    async def rung(*a, **k):
        climbed.append(a)

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", rung)
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", rung)

    await srv._force_escalate_stuck_hold()

    assert climbed == []
    await asyncio.sleep(0)
