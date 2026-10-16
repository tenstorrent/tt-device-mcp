# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The startup fabric pass waits for the 1G hugepage pool, and a hold whose only cause is a fabric
77 (could not check) is re-checked read-only, never escalated to a galaxy reset (spec 03 I33, I34).
A startup pass that 77'd for want of the validator or the pool is owed again once they land, even
with the fabric relift switched off (I39).

box A, 2026-10-08: the broker started before tenstorrent-hugepages.service had allocated the pool,
the startup fabric pass exited 77 with its reason cut to "no link was tested", and the idle relift
then escalated that hold to a glx_reset 120 s later — a reset that cannot produce a fabric verdict.
"""

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import fsm_dirty, patch_health_event, patch_recovery
from tt_device_mcp import server as srv
from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.fsm import ServerState
from tt_device_mcp.health import evidence
from tt_device_mcp.health import monitor as health_monitor_mod
from tt_device_mcp.health.monitors import hostpci, pci
from tt_device_mcp.health.recovery import galaxy as galaxy_mod

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
async def test_a_failed_fabric_run_keeps_its_full_output_in_the_action_log(monkeypatch, tmp_path, clear_job_state):
    """The validator names the cause near the head of a long run and the verdict at its tail: the row
    keeps both. Fails on base, which kept only the last 8000 chars and lost the first error line."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path)
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    patch_health_event(monkeypatch, lambda *a, **k: None)
    monkeypatch.setenv(
        "TT_DEVICE_MCP_FABRIC_CHECK_CMD",
        "echo 'first error: link 3 down'; for i in $(seq 2000); do echo \"progress line $i padding padding\"; done; "
        "echo 'fabric-check: verdict at the tail'; exit 1",
    )

    ok, _detail = await srv.health_monitor.verify_fabric_health(timeout_sec=30)

    assert ok is False
    logs = [p.read_text() for p in tmp_path.rglob("*") if p.is_file()]
    row = [t for t in logs if "first error: link 3 down" in t]
    assert row, "the head of a long failed run, where the validator names the cause, must be in its action log"
    assert "fabric-check: verdict at the tail" in row[0] and "progress line 1000 " in row[0]


@pytest.mark.asyncio
async def test_a_77_run_keeps_its_stderr_tail_and_exit_code_in_an_incident_bundle(
    monkeypatch, tmp_path, clear_job_state
):
    """A 77 that aborted before any link test named its cause only on stderr, and nothing kept it:
    the cause took a rerun to read. Its tail and exit code now go to an incident bundle the detail
    names; a rerun with the same reason reuses that bundle instead of rotating others out. Fails on
    base, which wrote no bundle."""
    monkeypatch.setattr(srv, "job_log_dir", tmp_path / "jobs")
    monkeypatch.setattr(srv, "jobs", {})
    monkeypatch.setattr(srv, "_action_row", None, raising=False)
    monkeypatch.setattr(srv.health_monitor, "_fabric_skip_incident", None, raising=False)
    monkeypatch.setattr(evidence, "_kernel_evidence", lambda: "(kernel log stubbed)")
    patch_health_event(monkeypatch, lambda *a, **k: None)
    pad_line = "p" * 99 + "\n"
    pad = health_monitor_mod.FABRIC_SKIP_OUTPUT_TAIL_BYTES // len(pad_line) + 10
    monkeypatch.setenv(
        "TT_DEVICE_MCP_FABRIC_CHECK_CMD",
        f"echo 'fabric-check: head line'; for i in $(seq {pad}); do echo {'p' * 99}; done; "
        "echo '  what():  Hugepages are not allocated for channel: 0' >&2; "
        f"echo 'fabric-check: no link was tested -> CANNOT CHECK' >&2; exit {FABRIC_CHECK_CANNOT_CHECK_RC}",
    )

    ok, detail = await srv.health_monitor.verify_fabric_health(timeout_sec=30)

    assert ok is None
    bundles = sorted((evidence.health_dir() / evidence.INCIDENTS_DIR).glob("*_fabric_cannot_check_*"))
    assert len(bundles) == 1, "a 77 run must leave an incident bundle"
    assert f"exit {FABRIC_CHECK_CANNOT_CHECK_RC}, output in {bundles[0]}/fabric.log" in detail
    log = (bundles[0] / "fabric.log").read_text()
    assert "what():  Hugepages are not allocated for channel: 0" in log, "the stderr tail names the cause"
    assert log.rstrip().endswith("CANNOT CHECK")
    assert "fabric-check: head line" not in log and "bytes cut" in log, "only the tail is kept"
    assert len(log.encode()) <= health_monitor_mod.FABRIC_SKIP_OUTPUT_TAIL_BYTES + 64
    meta = json.loads((bundles[0] / "incident.json").read_text())
    assert meta["evidence"]["rc"] == FABRIC_CHECK_CANNOT_CHECK_RC
    assert "Hugepages are not allocated" in meta["evidence"]["reason"]

    ok, again = await srv.health_monitor.verify_fabric_health(timeout_sec=30)

    assert ok is None
    assert sorted((evidence.health_dir() / evidence.INCIDENTS_DIR).glob("*_fabric_cannot_check_*")) == bundles
    assert str(bundles[0]) in again


def test_only_output_past_the_ceiling_is_cut_and_both_ends_kept():
    big = "H" * health_monitor_mod.ACTION_LOG_OUTPUT_CHARS + "T" * 10
    out = health_monitor_mod._action_log_output(big)
    assert out.startswith("H") and out.endswith("T" * 10) and "chars cut" in out
    assert health_monitor_mod._action_log_output("short") == "short"


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
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    watchers = []

    async def fake_watch(missing):
        watchers.append(missing)

    monkeypatch.setattr(srv, "_await_startup_prerequisites", fake_watch)
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
async def test_a_startup_hugepages_hold_with_links_measured_down_stays_77_only(monkeypatch, _startup):
    """A reset before the pool exists could only 77 again: the hugepages hold waits, links down or not."""
    _shortfalls(monkeypatch, [(5, 32)])
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.05)
    monkeypatch.setattr(srv.health_monitor, "last_eth_link_drop", "228 of 256 eth link(s) up")
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)

    await srv._verify_fabric_on_start()

    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert srv._fabric_77_eth_drop() == ""
    assert srv._fabric_77_only_hold()
    assert srv._fabric_no_reset_hold() == "fabric check could not run (77)"


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


def _hold_77(monkeypatch, tmp_path, *, last_fabric_ok=None, due=True):
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
    monkeypatch.setattr(srv, "_fabric_77_recheck_since", None)
    monkeypatch.setattr(srv, "_fabric_77_rechecks", 0)
    monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", 0.0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", False)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", "")
    if due:  # the I48 backoff has its own tests below; the rest exercise a re-check that is due
        monkeypatch.setattr(srv, "_fabric_77_recheck_due", lambda: True)
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


# --- I48: a 77-only hold re-checks on a backoff ------------------------------------------------


@pytest.mark.parametrize(
    "raw,steps",
    [
        (None, [120, 300, 600, 1200]),
        ("90,240", [90, 240]),
        ("5,5000", [60, 1200]),
        ("2m,5m", [120, 300, 600, 1200]),
        (" , ", [120, 300, 600, 1200]),
    ],
)
def test_the_77_backoff_parses_and_clamps(monkeypatch, raw, steps):
    """Each step clamps to 60..1200 s; a malformed or empty value reads as the default."""
    if raw is None:
        monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raising=False)
    else:
        monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raw)
    assert srv._fabric_77_backoff_sec() == steps


def _opened_ago(monkeypatch, seconds):
    since = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, since=since))


@pytest.mark.asyncio
async def test_the_77_only_relift_reruns_the_pass_only_once_its_step_is_due(monkeypatch, tmp_path, clear_job_state):
    """I48: before the first step the relift skips the pass; once due it runs it once, and the 77 it
    reads again moves the next re-check to the second step."""
    _hold_77(monkeypatch, tmp_path, due=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raising=False)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", "99")
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    async def relift():
        monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
        await srv._attempt_idle_relift()

    _opened_ago(monkeypatch, 30)
    await relift()
    assert seen == [], "the first re-check waits for the first step"
    _opened_ago(monkeypatch, 130)
    await relift()
    assert seen == [True], "due: the pass runs once"
    await relift()
    assert seen == [True], "the next re-check waits for the second step"
    monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", srv.time.monotonic() - 301)
    await relift()
    assert seen == [True, True]
    assert srv.fsm.state is ServerState.RECOVERING


def test_a_77_recheck_advances_the_backoff_step(monkeypatch, tmp_path, clear_job_state):
    """Each re-check that 77s again moves to the next step; the last step repeats; a new episode starts over."""
    _hold_77(monkeypatch, tmp_path, due=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raising=False)

    def due_after(seconds):
        monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", srv.time.monotonic() - seconds)
        return srv._fabric_77_recheck_due()

    srv._note_fabric_77_recheck()
    srv._note_fabric_77_recheck()
    assert srv._fabric_77_rechecks == 2
    assert not due_after(590) and due_after(610), "the third re-check waits the third step"
    for _ in range(5):
        srv._note_fabric_77_recheck()
    assert not due_after(1190) and due_after(1210), "the last step repeats"
    _opened_ago(monkeypatch, 30)
    srv._note_fabric_77_recheck()
    assert srv._fabric_77_rechecks == 1, "a new episode starts its backoff over"


_HUGEPAGE_77S = [
    "fabric-check: hugepage pool not ready: 0/32 1G pages allocated -> CANNOT CHECK (exit 77)",
    "  what():  Failed to pin pages for hugepage\nfabric-check: validator did not complete a measurement",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", _HUGEPAGE_77S, ids=["wrapper-precheck", "umd-pin-failure"])
async def test_a_hugepage_77_rechecks_every_30s_and_spends_no_retry(monkeypatch, tmp_path, clear_job_state, output):
    """Boot fills the 1G pool after the broker's startup pass ran: a 77 for that re-checks after 30 s,
    not on the 120 s relift interval or the I48 backoff, and its re-checks do not spend the I40 retries
    that would release a box no job can open yet."""
    _hold_77(monkeypatch, tmp_path, due=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BACKOFF_SEC", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", output)
    fallbacks = []

    async def fallback(*a, **k):
        fallbacks.append(True)
        return True

    monkeypatch.setattr(srv, "_fabric_77_fallback", fallback)
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": output}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    def ago(seconds):
        return srv.time.monotonic() - seconds

    _opened_ago(monkeypatch, 20)
    await srv._attempt_idle_relift()
    assert seen == [], "not yet 30 s since the hold opened"
    _opened_ago(monkeypatch, 35)
    await srv._attempt_idle_relift()
    assert seen == [True], "30 s after the hold opened the pass runs, not 120 s"
    for n in range(2, 6):
        monkeypatch.setattr(srv, "last_relift_monotonic", ago(20))
        monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", ago(20))
        await srv._attempt_idle_relift()
        assert len(seen) == n - 1, "under 30 s since the last re-check: no pass"
        monkeypatch.setattr(srv, "last_relift_monotonic", ago(31))
        monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", ago(31))
        await srv._attempt_idle_relift()
        assert len(seen) == n, "each later re-check comes 30 s after the last, below the relift interval"
    assert srv._fabric_77_streak == 0 and fallbacks == [], "a pool still filling spends no I40 retry"
    assert srv.fsm.state is ServerState.RECOVERING

    # Any other 77 keeps the I48 backoff and the relift interval.
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", "fabric-check: validator did not complete")
    monkeypatch.setattr(srv, "last_relift_monotonic", ago(31))
    monkeypatch.setattr(srv, "_fabric_77_recheck_monotonic", ago(31))
    await srv._attempt_idle_relift()
    assert len(seen) == 5


@pytest.mark.parametrize("hugepages", [True, False], ids=["hugepage-77", "other-77"])
@pytest.mark.parametrize("age,reached", [(100, False), (700, True), (7200, True)])
def test_the_77_age_bound_counts_from_the_fsm_stamp(monkeypatch, tmp_path, clear_job_state, hugepages, age, reached):
    """I40's age bound ends a hold the retries do not: a hugepage 77 spends none, so only the age
    ends it. The bound reads the FSM's own ``...Z`` stamp."""
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BOUND_SEC", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(
        srv.health_monitor,
        "last_fabric_output",
        _HUGEPAGE_77S[0] if hugepages else "fabric-check: validator did not complete",
    )
    _opened_ago(monkeypatch, age)
    assert srv._fabric_77_bound_reached() is reached
    assert srv._fabric_77_streak == (0 if hugepages else 1), "one 77, under the retry bound"


def test_an_unparseable_stamp_falls_back_to_the_retry_count(monkeypatch, tmp_path, clear_job_state):
    """A ``since`` the bound cannot read must not hold forever: each 77, a hugepage one too, then
    counts as a retry, so the retry bound still ends the hold."""
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", _HUGEPAGE_77S[0])
    monkeypatch.setattr(srv.fsm, "_record", dataclasses.replace(srv.fsm.record, since="not a stamp"))
    assert [srv._fabric_77_bound_reached() for _ in range(3)] == [False, False, True]


@pytest.mark.asyncio
async def test_a_hugepage_77_hold_past_its_age_bound_takes_the_fallback(monkeypatch, tmp_path, clear_job_state):
    """A pool that never fills does not hold forever: past the age bound the relift's hugepage 77
    hands over to the I40 fallback."""
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_BOUND_SEC", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", _HUGEPAGE_77S[0])
    fallbacks = []

    async def fallback(*a, **k):
        fallbacks.append(True)
        return True

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": _HUGEPAGE_77S[0]}}

    monkeypatch.setattr(srv, "_fabric_77_fallback", fallback)
    patch_recovery(monkeypatch, "_verify_device", verify)
    _opened_ago(monkeypatch, 2 * 3600)
    await srv._attempt_idle_relift()
    assert fallbacks == [True]


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


# --- a 77 on eth links measured down is a fault, not a missing verdict -------------------------
#
# A host, 2026-10-14: chips came back by rescan without a reset, the post-job eth read measured 228
# of 256 links and the fabric validator exited 77 (it cannot build a mesh with links missing). The
# hold was read as 77-only, so the relift re-ran the pass on its backoff for about 17 minutes before
# the I40 bound sent it to the reset that fixed it.

_DROP = "228 of 256 eth link(s) up — a link went down since the high-water mark"


def _hold_77_eth(monkeypatch, tmp_path, *, drop=_DROP, due=True):
    _hold_77(monkeypatch, tmp_path, due=due)
    monkeypatch.setattr(srv.health_monitor, "last_eth_link_drop", drop)
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "")
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_RELIFT", raising=False)


def test_the_eth_read_records_a_measured_link_drop(monkeypatch, tmp_path):
    monkeypatch.setattr(health_monitor_mod, "health_dir", lambda: tmp_path)
    m = srv.health_monitor
    monkeypatch.setattr(m, "last_eth_link_drop", "")
    (tmp_path / m.ETH_LINK_BASELINE_FILE).write_text(json.dumps({"links": 256}))
    assert m.eth_link_drop(228)
    assert "228 of 256" in m.last_eth_link_drop
    assert m.eth_link_drop(256) == ""
    assert m.last_eth_link_drop == "", "a read back at the mark clears it"


@pytest.mark.asyncio
async def test_a_77_with_eth_links_down_escalates_on_the_first_relift(monkeypatch, tmp_path, clear_job_state):
    """F7: the first relift pass goes to the present-mesh rung; no fabric retries, no 77 bound."""
    _hold_77_eth(monkeypatch, tmp_path)
    assert not srv._fabric_77_only_hold()
    assert srv._fabric_no_reset_hold() == ""
    assert srv._idle_relift_armed() == (False, False, True)
    seen, rungs = [], []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"fabric": {"ok": None, "detail": "could not run (77)"}}

    async def escalate(rung, *a, **k):
        rungs.append(rung)
        return srv.OUTCOME_WAITING

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    await srv._attempt_idle_relift()
    assert rungs == ["present"]
    assert seen == [], "no fabric re-run on the backoff first"
    assert srv.fsm.state is ServerState.RECOVERING, "never released by the escalation call itself"


def test_a_77_with_eth_links_down_is_forced_past_the_hold_deadline(monkeypatch, tmp_path, clear_job_state):
    _hold_77_eth(monkeypatch, tmp_path)
    fired, since = _deadline(monkeypatch)
    srv._check_hold_deadline(now=since + timedelta(seconds=2 * srv._stuck_hold_ceiling_sec() + 1))
    assert fired, "a measured link drop is a fault the deadline escalation owns"


@pytest.mark.asyncio
async def test_a_77_with_eth_links_full_keeps_the_backoff(monkeypatch, tmp_path, clear_job_state):
    """256 of 256: a 77 is still a missing verdict. The relift waits for its I48 backoff step and
    never resets."""
    _hold_77_eth(monkeypatch, tmp_path, drop="", due=False)
    monkeypatch.setattr(srv, "_fabric_77_recheck_due", lambda: False)
    assert srv._fabric_77_only_hold()
    assert srv._fabric_no_reset_hold() == "fabric check could not run (77)"
    assert srv._idle_relift_armed() == (False, True, False)
    seen, rungs = [], []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"fabric": {"ok": None, "detail": "could not run (77)"}}

    async def escalate(rung, *a, **k):
        rungs.append(rung)

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    await srv._attempt_idle_relift()
    assert seen == [] and rungs == [], "the backoff step is not due: nothing runs"
    monkeypatch.setattr(srv, "_fabric_77_recheck_due", lambda: True)
    await srv._attempt_idle_relift()
    assert seen == [True] and rungs == [], "once due it re-runs the pass, never a reset"


@pytest.mark.asyncio
async def test_a_78_cable_fault_with_eth_links_down_holds_without_a_reset(monkeypatch, tmp_path, clear_job_state):
    """A 78 (a missing cable) reads as links down too, and no reset fixes a cable: it keeps its hold."""
    _hold_77_eth(monkeypatch, tmp_path)
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "fabric cable fault (fabric check exit 78)")
    assert srv._fabric_77_eth_drop() == ""
    assert srv._fabric_no_reset_hold() == "known fabric fault"
    climbed = []

    async def rung(*a, **k):
        climbed.append(a)

    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_stuck_hold", rung)
    monkeypatch.setattr(srv.galaxy_recovery, "_escalate_offbus_stuck_hold", rung)
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv, "dead_chips", lambda beats: [])
    monkeypatch.setattr(srv, "device_hold_episode_since", "2026-10-14T22:18:18")
    await srv._force_escalate_stuck_hold()
    assert climbed == [], "a 78 is held, never reset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_reset, released",
    [
        ({"ok": True, "detail": "fabric links healthy"}, True),
        ({"ok": None, "detail": "77"}, False),
        ({"ok": False, "detail": "2 links failed"}, False),
    ],
)
async def test_a_77_eth_drop_reset_releases_only_on_a_passing_full_check(
    monkeypatch, tmp_path, clear_job_state, post_reset, released
):
    """Goal 8: the reset this escalation runs is followed by the full health check, fabric included,
    and only a passing fabric verdict releases the device; a 77 or a failed pass keeps it held."""
    _hold_77_eth(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=21)).isoformat())
    resets, checks = _present_rung_chain(monkeypatch, post_reset)

    await srv._attempt_idle_relift()

    assert len(resets) == 1, "the first relift pass runs the present-mesh reset"
    assert checks == [True], "the post-reset check runs the fabric pass"
    assert (srv.fsm.state is ServerState.HEALTHY) is released
    if not released:
        assert srv._recovery_degraded(), "a post-reset check that did not pass keeps the device held"


def _present_rung_chain(monkeypatch, post_reset):
    """The real present-mesh rung down to the reset command, with the post-reset check answering
    ``post_reset`` as its fabric verdict. Returns the reset argvs and the run_fabric of each check."""
    from tt_device_mcp.health import recovery as recovery_pkg
    from tt_device_mcp.health.recovery import pcie_guard

    monkeypatch.setenv("TT_DEVICE_MCP_STUCK_HOLD_RESET", "1")
    monkeypatch.setenv("TT_DEVICE_MCP_RESET_MODE", "galaxy")
    monkeypatch.setattr(srv, "read_heartbeats", lambda: {str(i): 100 for i in range(32)})
    monkeypatch.setattr(srv, "enumerate_device_holders", lambda: srv.HolderScan(holders=[], complete=True))
    monkeypatch.setattr(srv.recovery_mechanism, "last_reset_monotonic", 0.0)
    monkeypatch.setattr(srv.recovery_mechanism, "unverified_reset_since", None)
    monkeypatch.setattr(recovery_pkg, "POST_RESET_FABRIC_RETRIES", 0)
    monkeypatch.setattr(recovery_pkg, "refuse_mesh_reset", lambda *a, **k: False)
    monkeypatch.setattr(pcie_guard, "chips_off_bus", lambda expected: 0)
    monkeypatch.setattr(pcie_guard, "host_reset_gate", lambda off_bus: (True, ""))
    monkeypatch.setattr(pcie_guard, "mask_for_mesh_reset", lambda log: None)
    monkeypatch.setattr(pcie_guard, "begin_offbus_reset", lambda *a, **k: None)
    monkeypatch.setattr(pcie_guard, "end_offbus_reset", lambda *a, **k: None)
    climbs, resets, checks = [], [], []

    async def no_foreign(log):
        return False

    async def reset(argv, log):
        resets.append(argv)
        return 0, ""

    async def verify(expected, log, run_fabric=True, **_):
        checks.append(run_fabric)
        return post_reset["ok"] is not False, {"snapshot": {"ok": True}, "fabric": post_reset}

    async def climb(*a, **k):
        climbs.append(a)

    monkeypatch.setattr(srv.recovery_mechanism, "await_foreign_scope", no_foreign)
    monkeypatch.setattr(srv.recovery_mechanism, "reset_with_quiesce", reset)
    monkeypatch.setattr(srv.galaxy_recovery, "_climb_to_host_recovery_after_failed_reset", climb)
    patch_recovery(monkeypatch, "_verify_device", verify)
    return resets, checks


# --- a 77 from a validator crash gets one reset per episode (I62) -------------------------------
#
# A Galaxy host, 2026-10-14: after a rescan brought chips back, every post-job fabric pass exited
# 77 with "what():  unordered_map::at". The relift re-ran it for the I40 bound and the hold ended
# only with a host reboot after 26.6 min, while an operator glx_reset six minutes earlier had healed the
# same state in under two minutes. The gate's eth read had measured 228 of 256 links (I53); a crash
# with no link count recorded (no eth reader, or a read that cleared the drop) must still reach the
# reset, once.

_CRASH = "Validating fabric...\nterminate called after throwing an instance of 'std::out_of_range'\n  what():  unordered_map::at"
_SINCE = "2026-10-14T15:18:18.000001"


def _hold_77_crash(monkeypatch, tmp_path, *, output=_CRASH, drop="", since=_SINCE):
    _hold_77_eth(monkeypatch, tmp_path, drop=drop)
    srv.fsm.record.detail = "gate/post-job: enum+ARC healthy, fabric unverified (rc 77)"
    monkeypatch.setattr(srv.health_monitor, "last_fabric_output", output)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", True)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    monkeypatch.setattr(srv, "device_hold_episode_since", since)
    monkeypatch.setattr(srv.recovery_mechanism, "unverified_reset_since", None)


def _record_rungs(monkeypatch):
    seen, rungs = [], []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"fabric": {"ok": None, "detail": "could not run (77)"}}

    async def escalate(rung, *a, **k):
        rungs.append(rung)
        return srv.OUTCOME_WAITING

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    return seen, rungs


@pytest.mark.asyncio
async def test_the_replayed_crash_hold_with_links_down_escalates_present(monkeypatch, tmp_path, clear_job_state):
    """That hold as the gate left it: the eth read's drop is still recorded, so I53 alone
    sends it to the present-mesh rung on the first relift."""
    _hold_77_crash(monkeypatch, tmp_path, drop=_DROP)
    assert srv._fabric_77_eth_drop() == _DROP
    seen, rungs = _record_rungs(monkeypatch)
    await srv._attempt_idle_relift()
    assert rungs == ["present"] and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [_CRASH, "what(): map::at", "rc=77\nwhat():\tunordered_map::at\n"],
)
async def test_a_validator_crash_77_escalates_on_the_first_relift(monkeypatch, tmp_path, clear_job_state, output):
    _hold_77_crash(monkeypatch, tmp_path, output=output)
    assert srv._fabric_77_eth_drop() == ""
    assert "validator crashed" in srv._fabric_77_validator_crash()
    assert not srv._fabric_77_only_hold()
    assert srv._fabric_no_reset_hold() == ""
    assert srv._idle_relift_armed() == (False, False, True)
    seen, rungs = _record_rungs(monkeypatch)
    await srv._attempt_idle_relift()
    assert rungs == ["present"], "the present-mesh rung, not the I48 backoff"
    assert seen == [], "no fabric re-run first"
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_reset, released",
    [
        ({"ok": True, "detail": "fabric links healthy"}, True),
        ({"ok": None, "detail": "what():  unordered_map::at"}, False),
        ({"ok": False, "detail": "2 links failed"}, False),
    ],
)
async def test_the_crash_reset_runs_the_full_check_and_releases_only_on_a_pass(
    monkeypatch, tmp_path, clear_job_state, post_reset, released
):
    """The rung fires once the 120 s present-mesh grace has passed, well inside the 5 min D77 budget;
    goal 8: the reset is followed by the full check, fabric included, and only ok=True releases."""
    since = (datetime.now() - timedelta(seconds=galaxy_mod._present_mesh_reset_grace_sec() + 5)).isoformat()
    assert galaxy_mod._present_mesh_reset_grace_sec() <= 120
    _hold_77_crash(monkeypatch, tmp_path, since=since)
    resets, checks = _present_rung_chain(monkeypatch, post_reset)

    await srv._attempt_idle_relift()

    assert len(resets) == 1, "the first relift pass past the grace runs the present-mesh reset"
    assert checks == [True], "the post-reset check runs the fabric pass"
    assert (srv.fsm.state is ServerState.HEALTHY) is released
    if not released:
        assert srv._recovery_degraded(), "a post-reset check that did not pass keeps the device held"


@pytest.mark.asyncio
async def test_a_validator_crash_77_gets_no_second_reset_after_an_unverified_one(
    monkeypatch, tmp_path, clear_job_state
):
    """The episode's reset came back unverified (a 77 again): the crash is a missing verdict now. It
    keeps the I48 backoff and the I40 bound and its fallback; this rule never resets it again."""
    _hold_77_crash(monkeypatch, tmp_path)
    resets, checks = _present_rung_chain(monkeypatch, {"ok": None, "detail": "what():  unordered_map::at"})
    monkeypatch.setattr(srv, "device_hold_episode_since", (datetime.now() - timedelta(minutes=3)).isoformat())
    await srv._attempt_idle_relift()
    assert len(resets) == 1
    assert srv.recovery_mechanism.unverified_reset_since == srv.device_hold_episode_since
    srv.health_monitor.last_fabric_output = _CRASH
    srv.health_monitor.last_fabric_skipped = True
    srv.health_monitor.last_fabric_ok = None
    assert srv._fabric_77_validator_crash() == ""
    assert srv._fabric_77_only_hold()
    assert srv._fabric_no_reset_hold() == "fabric check could not run (77)"
    assert srv._idle_relift_armed() == (False, True, False)
    seen, rungs = _record_rungs(monkeypatch)
    monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
    monkeypatch.setattr(srv, "_fabric_77_bound_reached", lambda: False)  # past it, the I40 fallback owns it
    await srv._attempt_idle_relift()
    assert rungs == [] and seen == [True], "the pass re-runs on its backoff, never a second reset"


@pytest.mark.parametrize(
    "exclusion",
    ["cable", "topology_fault", "topology_mismatch", "hugepages", "validator_missing", "no_episode", "not_skipped"],
)
def test_a_validator_crash_77_keeps_its_no_reset_exclusions(monkeypatch, tmp_path, clear_job_state, exclusion):
    _hold_77_crash(monkeypatch, tmp_path)
    assert srv._fabric_77_validator_crash()
    if exclusion == "cable":
        monkeypatch.setattr(
            srv.health_monitor, "known_fabric_fault", lambda: "fabric cable fault (fabric check exit 78)"
        )
    elif exclusion == "topology_fault":
        monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "chip 3 tray 1 port 2 down")
    elif exclusion == "topology_mismatch":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_output", _CRASH + "\n" + srv._TOPOLOGY_VALIDATOR_LINE)
    elif exclusion == "hugepages":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_output", _CRASH + "\nhugepage pool not ready")
    elif exclusion == "validator_missing":
        monkeypatch.setattr(srv.fabric, "validator_missing", lambda: "no validator binary")
    elif exclusion == "no_episode":
        monkeypatch.setattr(srv, "device_hold_episode_since", "")
    elif exclusion == "not_skipped":
        monkeypatch.setattr(srv.health_monitor, "last_fabric_skipped", False)
    assert srv._fabric_77_validator_crash() == ""
    assert not srv._idle_relift_armed()[2], "never the generic present-mesh escalation"


# --- a startup pass the validator missed is owed again once it lands (I39) ----------------------
#
# box B, 2026-10-11: the broker started 2 min after boot, the validator timer published the
# pinned build's `current` link 8 min later, and the startup pass exited 77 in between. With the
# fabric relift switched off by a hold drop-in, nothing re-ran it and a healthy mesh stayed held
# until someone restarted the broker.


def _validator(tmp_path, monkeypatch, *, binary=True, descriptor=None):
    root = tmp_path / "validator"
    current = root / "current"
    if binary:
        b = current / "build" / "tools" / "scaleout" / "run_cluster_validation"
        b.parent.mkdir(parents=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    monkeypatch.setenv("TTDEV_VALIDATOR_ROOT", str(root))
    monkeypatch.setenv("TTDEV_FABRIC_CACHE", str(tmp_path / "cache"))
    for k in ("TTDEV_FABRIC_BIN", "TTDEV_FABRIC_RUNTIME_ROOT", "TT_DEVICE_MCP_FABRIC_CHECK_CMD"):
        monkeypatch.delenv(k, raising=False)
    if descriptor is None:
        monkeypatch.delenv("TTDEV_FABRIC_DESCRIPTOR", raising=False)
    else:
        monkeypatch.setenv("TTDEV_FABRIC_DESCRIPTOR", str(current / descriptor))
    return current


def test_validator_missing_names_what_is_not_installed(tmp_path, monkeypatch):
    _validator(tmp_path, monkeypatch, binary=False)
    assert "pinned validator not installed yet" in srv.fabric.validator_missing()
    assert srv.fabric.build_command() is None


def test_validator_missing_is_none_once_the_install_lands(tmp_path, monkeypatch):
    current = _validator(tmp_path, monkeypatch)
    assert srv.fabric.validator_missing() is None
    argv, env = srv.fabric.build_command()
    assert argv[0] == str(current / "build/tools/scaleout/run_cluster_validation")
    assert "--cabling-descriptor-path" not in argv
    assert env["TT_METAL_RUNTIME_ROOT"] == str(current)


def test_validator_missing_waits_for_a_configured_descriptor(tmp_path, monkeypatch):
    """The descriptor can live under the same `current` link, so it lands with it."""
    current = _validator(tmp_path, monkeypatch, descriptor="galaxy.textproto")
    assert "cabling descriptor" in srv.fabric.validator_missing()
    assert srv.fabric.build_command() is None
    (current / "galaxy.textproto").write_text("")
    assert srv.fabric.validator_missing() is None
    argv, _ = srv.fabric.build_command()
    assert argv[1:3] == ["--cabling-descriptor-path", str(current / "galaxy.textproto")]


def _startup_77_gate(monkeypatch, gate_calls):
    async def gate(job_log_file, *, phase, run_fabric, force_fabric=False):
        gate_calls.append(phase)
        srv.fsm.on_fault(srv.FABRIC_RELIFT_WHY, detail="gate/startup: fabric unverified", dirty=False)

    monkeypatch.setattr(srv, "_device_health_gate", gate)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)


@pytest.mark.asyncio
async def test_a_startup_77_without_the_validator_starts_the_watch(monkeypatch, _startup):
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [None])
    _startup_77_gate(monkeypatch, gate_calls)
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: "pinned validator not installed yet ('x')")
    watchers = []

    async def watch(missing):
        watchers.append(missing)

    monkeypatch.setattr(srv, "_await_startup_prerequisites", watch)

    await srv._verify_fabric_on_start()
    await srv._startup_prereq_task

    assert gate_calls == ["startup"], "the pass still runs at once"
    assert watchers == ["pinned validator not installed yet ('x')"]


@pytest.mark.asyncio
async def test_a_startup_77_with_the_validator_present_starts_no_watch(monkeypatch, _startup):
    """With the validator there the only added cost is the stat calls; a 77 for any other reason is
    the idle relift's, as before."""
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [None])
    _startup_77_gate(monkeypatch, gate_calls)
    monkeypatch.setattr(srv, "_startup_prereq_task", None)
    watchers = []

    async def watch(missing):
        watchers.append(missing)

    monkeypatch.setattr(srv, "_await_startup_prerequisites", watch)

    await srv._verify_fabric_on_start()

    assert gate_calls == ["startup"]
    assert srv._startup_prereq_task is None and watchers == []


@pytest.mark.asyncio
async def test_a_startup_hugepages_timeout_starts_the_watch(monkeypatch, _startup):
    events, rows, gate_calls = _startup
    _shortfalls(monkeypatch, [(5, 32)])
    monkeypatch.setattr(srv, "STARTUP_HUGEPAGES_WAIT_SEC", 0.05)
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", None)
    watchers = []

    async def watch(missing):
        watchers.append(missing)

    monkeypatch.setattr(srv, "_await_startup_prerequisites", watch)

    await srv._verify_fabric_on_start()
    await srv._startup_prereq_task

    assert gate_calls == []
    assert watchers == ["hugepages not yet allocated"]


def _sleeps(monkeypatch):
    """Record the watcher's sleeps instead of taking them; ``on_sleep`` runs after each one."""
    slept = []
    hooks = []

    async def fake_sleep(sec):
        slept.append(sec)
        for h in hooks:
            h(len(slept))

    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)
    return slept, hooks


@pytest.mark.asyncio
async def test_the_watch_owes_the_pass_once_the_validator_lands(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    landed = {"v": False}
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None if landed["v"] else "not installed")
    slept, hooks = _sleeps(monkeypatch)
    hooks.append(lambda n: landed.update(v=n >= 6))

    assert srv._idle_relift_armed() == (False, False, False), "relift off: the hold would just stand"
    await srv._await_startup_prerequisites("not installed")

    assert slept == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0], "backs off to a 60 s look"
    assert srv._startup_fabric_owed()
    assert srv._idle_relift_armed() == (False, True, False), "the relift now owes the one pass"


@pytest.mark.asyncio
async def test_the_watch_also_waits_for_the_hugepages(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: None)
    _shortfalls(monkeypatch, [(0, 32), (0, 32), None])
    slept, _ = _sleeps(monkeypatch)

    await srv._await_startup_prerequisites("hugepages not yet allocated")

    assert len(slept) == 2
    assert srv._startup_fabric_owed()


@pytest.mark.asyncio
async def test_the_watch_stops_once_the_hold_is_settled_elsewhere(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: "not installed")
    slept, hooks = _sleeps(monkeypatch)
    hooks.append(lambda n: n == 2 and srv._clear_device_dirty(verified=True, why="a job's gate verified it"))

    await srv._await_startup_prerequisites("not installed")

    assert len(slept) == 2
    assert not srv._startup_fabric_owed()


@pytest.mark.asyncio
async def test_the_watch_gives_up_after_its_bound(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", None)
    monkeypatch.setattr(srv, "STARTUP_PREREQ_WAIT_SEC", 0.0)
    monkeypatch.setattr(srv.fabric, "validator_missing", lambda: "not installed")
    slept, _ = _sleeps(monkeypatch)

    await srv._await_startup_prerequisites("not installed")

    assert slept == []
    assert not srv._startup_fabric_owed()
    assert srv._idle_relift_armed() == (False, False, False)


@pytest.mark.asyncio
async def test_the_owed_pass_runs_once_with_the_relift_off_and_lifts_on_healthy(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", srv.fsm.record.since)
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": True, "detail": "fabric healthy"}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)

    await srv._attempt_idle_relift()

    assert seen == [True], "the owed startup pass runs the fabric check"
    assert rungs == [], "and never resets"
    assert srv.fsm.state is ServerState.HEALTHY, "a healthy verdict lifts the hold"
    assert not srv._startup_fabric_owed()


@pytest.mark.asyncio
async def test_the_owed_pass_is_owed_once(monkeypatch, tmp_path, clear_job_state):
    """Another 77 on the owed pass leaves the hold standing, and with the relift off nothing re-runs it."""
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", srv.fsm.record.since)
    seen = []

    async def verify(expected, log, run_fabric=True, **_):
        seen.append(run_fabric)
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    patch_recovery(monkeypatch, "_verify_device", verify)

    await srv._attempt_idle_relift()

    assert seen == [True]
    assert srv.fsm.record.why == srv.FABRIC_RELIFT_WHY
    assert srv._idle_relift_armed() == (False, False, False)


def test_an_owed_pass_does_not_carry_to_another_episode(monkeypatch, tmp_path, clear_job_state):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_RELIFT", "0")
    monkeypatch.setattr(srv, "_startup_fabric_owed_since", "an earlier episode")
    assert not srv._startup_fabric_owed()
    assert srv._idle_relift_armed() == (False, False, False)


# --- I40: a 77-only hold is bounded, then the startup checks are the gate -----------------------


def _bound(monkeypatch, tmp_path, eth):
    _hold_77(monkeypatch, tmp_path)
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", "1")
    monkeypatch.setattr(srv, "_fabric_77_fallback_release_monotonic", 0.0)
    monkeypatch.setattr(srv.alert, "send_alert", lambda *a, **k: None)

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": "could not run (77)"}}

    async def eth_read(*a, **k):
        return eth

    patch_recovery(monkeypatch, "_verify_device", verify)
    monkeypatch.setattr(srv.health_monitor, "verify_eth_heartbeat", eth_read)
    rungs = []

    async def escalate(rung, *a, **k):
        rungs.append(rung)
        return srv.OUTCOME_WAITING

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    return rungs


@pytest.mark.asyncio
async def test_a_77_only_hold_past_its_bound_lifts_on_a_healthy_eth_read(monkeypatch, tmp_path, clear_job_state):
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    await srv._attempt_idle_relift()
    assert rungs == []
    assert srv.fsm.state is ServerState.HEALTHY


@pytest.mark.asyncio
async def test_a_77_only_hold_with_a_link_drop_climbs_the_ladder_not_released(monkeypatch, tmp_path, clear_job_state):
    """A 77 is never a healthy fabric: a link-count drop reads no-verdict and goes to the ladder."""
    rungs = _bound(monkeypatch, tmp_path, (None, "eth links below baseline"))
    await srv._attempt_idle_relift()
    assert rungs == ["present"]
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_second_77_only_hold_inside_the_window_climbs_the_ladder(monkeypatch, tmp_path, clear_job_state):
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setattr(srv, "_fabric_77_fallback_release_monotonic", srv.time.monotonic())
    await srv._attempt_idle_relift()
    assert rungs == ["present"]
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_the_77_bound_pages_once_per_hold_episode(monkeypatch, tmp_path, clear_job_state):
    """Every relift pass past the bound runs the fallback again (here each climbs a ladder that leaves
    the hold standing). Only the first on an episode pages. A page the hook dropped (a run still in
    flight) goes out on the next pass, and a page sent for an earlier episode does not quiet this one."""
    rungs = _bound(monkeypatch, tmp_path, (None, "eth links below baseline"))
    monkeypatch.setattr(srv.alert, "alert_argv", lambda: ["hook"])
    monkeypatch.setattr(srv, "_fabric_77_paged_since", "an earlier episode", raising=False)
    sent = []
    outcomes = iter([None])  # the first send is dropped; every later one starts

    def send_alert(event, log=None):
        if event["kind"] == "fabric_check_unrunnable_bound":
            sent.append(event["held_since"])
            return next(outcomes, object())
        return object()

    monkeypatch.setattr(srv.alert, "send_alert", send_alert)
    for _ in range(4):
        monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
        await srv._attempt_idle_relift()

    assert rungs == ["present"] * 4, "every pass past the bound still runs the fallback"
    assert sent == [srv.fsm.record.since] * 2, "one dropped page, then one page for the episode"


@pytest.mark.asyncio
async def test_the_topology_hw_fault_hold_repages_once_per_interval_per_episode(monkeypatch, tmp_path, clear_job_state):
    """I42: the first-job hardware-fault hold stands until an operator restarts the broker, so the
    idle relift pages it again every TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC. The fault's own page counts
    for the episode, a dropped re-page goes out on the next pass, a new episode pages anew, and 0
    pages once."""
    _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    fault = "hardware fault: the first job after a topology-mismatch lift failed on the fabric (x)"
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", fault)
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault_paged", srv.time.monotonic())
    monkeypatch.setattr(srv, "_topology_hw_fault_paged", None)
    monkeypatch.setenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", "3600")
    monkeypatch.setattr(srv.alert, "alert_argv", lambda: ["hook"])
    sent = []
    outcomes = iter([None])  # the first re-page is dropped; every later one starts

    def send_alert(event, log=None):
        if event["kind"] == "fabric_topology_hw_fault":
            sent.append(event["held_since"])
            return next(outcomes, object())
        return object()

    monkeypatch.setattr(srv.alert, "send_alert", send_alert)

    async def relift(n):
        for _ in range(n):
            monkeypatch.setattr(srv, "last_relift_monotonic", 0.0)
            await srv._attempt_idle_relift()

    since = srv.fsm.record.since
    await relift(3)
    assert sent == [], "the fault's own page covers the episode until the interval runs out"
    srv._topology_hw_fault_paged = (since, srv.time.monotonic() - 3601)
    await relift(3)
    assert sent == [since, since], "one dropped re-page, then one re-page for the interval"
    assert srv.fsm.state is ServerState.RECOVERING

    srv._topology_hw_fault_paged = ("an earlier episode", srv.time.monotonic())
    await relift(2)
    assert sent[2:] == [since], "a page sent for an earlier episode does not quiet this one"

    monkeypatch.setenv("TT_DEVICE_MCP_MESH_HOLD_REPAGE_SEC", "0")
    srv._topology_hw_fault_paged = (since, srv.time.monotonic() - 99999)
    await relift(2)
    assert len(sent) == 3, "0 pages once per episode"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hold_detail,fabric_detail,flagged",
    [
        (
            "job 7: hardware fault: the first job after a topology-mismatch lift failed on the fabric (x)",
            "could not run (77)",
            "",
        ),
        (
            "gate/startup: fabric unverified (could not run)",
            "fabric check withheld: hardware fault: the first job after a topology-mismatch lift failed",
            "",
        ),
        ("gate/startup: fabric unverified (could not run)", "could not run (77)", "hardware fault: x"),
    ],
)
async def test_the_first_job_hardware_fault_is_outside_the_77_bound(
    monkeypatch, tmp_path, clear_job_state, hold_detail, fabric_detail, flagged
):
    """The first job after a topology lift proved the skipped links are needed: no release or rung
    can fix cabling, so past the bound it neither lifts nor climbs."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", flagged)
    fsm_dirty(srv, hold_detail, why="fabric_unverified")

    async def verify(expected, log, run_fabric=True, **_):
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": fabric_detail}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._attempt_idle_relift()
    assert rungs == []
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_passes, lifted", [(2, True), (0, False)])
async def test_after_an_unknown_reset_the_next_relift_77_reaches_the_bound(
    monkeypatch, tmp_path, clear_job_state, reset_passes, lifted
):
    """Spec 04 I29: a reset whose post-reset fabric stayed a 77 (one pass and one retry) counts those
    passes toward the episode's I40 bound, so the next relift's 77 falls back at once and releases on a
    healthy eth read. Without them, the same relift is still under the default bound and just holds."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.delenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", raising=False)
    monkeypatch.setattr(srv, "_fabric_77_streak_since", None)
    monkeypatch.setattr(srv, "_fabric_77_streak", 0)
    if reset_passes:
        srv._note_fabric_77_reset_passes(reset_passes)
    await srv._attempt_idle_relift()
    assert rungs == []
    assert (srv.fsm.state is ServerState.HEALTHY) is lifted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "eth,released",
    [((True, "all active-eth cores advancing"), True), ((None, "252 of 256 eth link(s) up"), False)],
)
async def test_a_topology_mismatch_whose_rerun_77s_takes_the_bounded_fallback(
    monkeypatch, tmp_path, clear_job_state, eth, released
):
    """I45: a topology-mismatch 77 is no longer an open-ended hold. Past the bound the eth read
    decides (against the re-baselined mark); a release is watched like a lift."""
    rungs = _bound(monkeypatch, tmp_path, eth)
    monkeypatch.setattr(srv.health_monitor, "topology_hw_fault", "")
    monkeypatch.setattr(srv.health_monitor, "topology_lift_pending", False)
    fsm_dirty(srv, "gate/startup: fabric unverified (could not run)", why="fabric_unverified")

    async def verify(expected, log, run_fabric=True, **_):
        detail = "fabric check COULD NOT RUN (19s): topology mismatch; traffic-only rerun: hugepages"
        return True, {"snapshot": {"ok": True}, "fabric": {"ok": None, "detail": detail}}

    patch_recovery(monkeypatch, "_verify_device", verify)
    await srv._attempt_idle_relift()
    if released:
        assert rungs == []
        assert srv.fsm.state is ServerState.HEALTHY
        assert srv.health_monitor.topology_lift_pending is True
    else:
        assert rungs == ["present"]
        assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_77_only_hold_under_its_bound_still_just_holds(monkeypatch, tmp_path, clear_job_state):
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setenv("TT_DEVICE_MCP_FABRIC_77_MAX_RETRIES", "5")
    await srv._attempt_idle_relift()
    assert rungs == []
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
async def test_a_known_fabric_fault_hold_is_outside_the_77_bound(monkeypatch, tmp_path, clear_job_state):
    """03 I46: the fallback release is what kept handing jobs a fabric that killed them, so a known
    fabric fault neither lifts on a healthy eth read nor climbs the ladder past the bound."""
    rungs = _bound(monkeypatch, tmp_path, (True, "all active-eth cores advancing"))
    monkeypatch.setattr(srv.health_monitor, "known_fabric_fault", lambda: "known fabric fault: 2 jobs in a row")
    await srv._attempt_idle_relift()
    assert rungs == []
    assert srv.fsm.state is ServerState.RECOVERING


@pytest.mark.asyncio
@pytest.mark.parametrize("last_fabric_ok, state", [(None, ServerState.RECOVERING), (False, ServerState.DOWN)])
async def test_a_terminal_rung_on_a_77_only_hold_keeps_it_recovering(
    monkeypatch, tmp_path, clear_job_state, last_fabric_ok, state
):
    """I48: a rung that ran and left the fabric only 77ing measured no fault, so the hold stays
    RECOVERING for the relift to re-check, never the terminal DOWN. A measured fault still goes DOWN."""
    rungs = _bound(monkeypatch, tmp_path, (None, "eth links below baseline"))
    monkeypatch.setattr(srv.health_monitor, "last_fabric_ok", last_fabric_ok)

    async def escalate(rung, *a, **k):
        rungs.append(rung)
        return srv.OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", escalate)
    await srv._attempt_idle_relift()
    assert rungs == ["present"]
    assert srv.fsm.state is state
