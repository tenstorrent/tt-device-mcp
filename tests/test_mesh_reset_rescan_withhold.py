# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""Spec 04 I69 on the mesh path: a failed mesh reset never re-admits a removed chip it did not reach.

A chip the broker removed (sysfs ``remove``) is reached by ``tt-smi -glx_reset`` only through the BMC
tray re-power. When that step did not go out on op_mode 0x3 (it failed, tt-smi fell back to 0x0, the
guard refused, or no reset ran) and the reset failed, a global PCI rescan would re-enumerate the hung,
un-reset chip: the rescan after the reset is withheld, journaled, and the hold climbs. The driver-wait
rescan (I81/I78) starts only once the output shows the re-power went out. Device-free.
"""

import asyncio
from pathlib import Path

import pytest

from tests.conftest import patch_recovery
from tests.test_device_safety import _arm_fabric_only_deadline_hold
from tests.test_hold_bound import _KINDS, BOUND
from tests.test_ipmi_guard import MESH, ladder  # noqa: F401 - the fixture
from tests.test_post_reset_gate import F77, FOK, _verify_returning
from tt_device_mcp import server as srv
from tt_device_mcp.health.monitors import pci
from tt_device_mcp.health.post_reset import FABRIC_DEBT
from tt_device_mcp.health.recovery import OUTCOME_TERMINAL, galaxy, ipmi_guard

RESCAN = Path("/sys/bus/pci/rescan")
_REAL_SLEEP = asyncio.sleep  # the fake launch yields for real, so the driver-wait watcher's task can start
REMOVED = "31"
# Nothing is enumerated at this address in the fake sysfs, so the record stands (chip still off the bus).
REMOVED_REC = {"bdf": "0000:ff:00.0", "bridge": "0000:fe:01.1"}
SETTLE = "\x1b[93mWaiting for 30 seconds: 0\x1b[0m" + "".join(
    f"\r\x1b[93mWaiting for 30 seconds: {i}\x1b[0m" for i in range(1, 31)
)
DRIVER_WAIT = 'Waiting for all 32 chips to show up in "/dev/tenstorrent", found 31\n'
FALLBACK = "WARNING! Galaxy no-retimer IPMI reset (op_mode 0x3) failed:\nFalling back to legacy retimer reset (op_mode 0x0)...\n"
IPMI_FAILED = "Issuing USER_RESET on 32 devices before IPMI reset...\nRuntimeError: IPMI command failed: rsp=0xc3\n"


def _count_rescans(monkeypatch) -> list:
    """Record every write to /sys/bus/pci/rescan (the real one never runs); other threads run inline."""
    writes: list = []

    async def to_thread(fn, *a, **k):
        if getattr(fn, "__self__", None) == RESCAN:
            writes.append(a)
            return None
        return fn(*a, **k)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(srv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    return writes


def _launch(monkeypatch, rig, rc, chunks):
    """A mesh reset over a stubbed launch that streams ``chunks`` and returns (rc, all of them)."""

    async def run_scoped(argv, log, owner="", on_output=None):
        rig["launches"].append(list(argv))
        for c in chunks:
            if on_output is not None:
                on_output(c)
            await _REAL_SLEEP(0)
        return rc, "".join(chunks)

    monkeypatch.setattr(rig["m"], "run_scoped", run_scoped)


def _withheld(rig):
    return [f for k, f in rig["events"] if k == "pci_rescan_withheld"]


@pytest.mark.parametrize(
    "text, ok",
    [
        (SETTLE + DRIVER_WAIT, True),
        (DRIVER_WAIT, True),
        ("\x1b[93mWaiting for 30 seconds: 0\x1b[0m", True),
        (IPMI_FAILED, False),
        ("", False),
        (FALLBACK + SETTLE + DRIVER_WAIT, False),
        (SETTLE + ipmi_guard.REFUSED_MARKER + ": op_mode 0x0\n", False),
        (ipmi_guard.NOT_ARMED_MARKER + ": x\n", False),
    ],
    ids=["settle+wait", "wait", "settle-start", "ipmi-failed", "no-output", "fallback", "refused", "not-armed"],
)
def test_bmc_step_ok_reads_the_re_power_from_the_output(text, ok):
    assert ipmi_guard.bmc_step_ok(text) is ok


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rc, chunks",
    [
        (1, [IPMI_FAILED]),
        (1, [FALLBACK, ipmi_guard.REFUSED_MARKER + ": op_mode 0x0\n"]),
        (0, [FALLBACK, SETTLE, DRIVER_WAIT]),
        (1, [ipmi_guard.NOT_ARMED_MARKER + ": x; mesh reset NOT run\n"]),
        (None, []),
    ],
    ids=["bmc-failed", "fallback-refused", "fallback-sent-rc0", "guard-not-armed", "launch-failed"],
)
async def test_a_failed_mesh_reset_withholds_the_rescan_of_an_unreset_removed_chip(
    ladder, monkeypatch, rc, chunks  # noqa: F811
):
    rig = ladder
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)
    srv.isolated_chips = {REMOVED}
    writes = _count_rescans(monkeypatch)
    _launch(monkeypatch, rig, rc, chunks)
    got, _ = await rig["m"].reset_with_quiesce(MESH, lambda m: None)
    assert got != 0
    assert writes == [], "no global rescan re-enumerates the removed chip the reset did not reach"
    after = [f for f in _withheld(rig) if "at" not in f]
    assert len(after) == 1 and after[0]["reason"] == "mesh_reset_failed"
    assert after[0]["chips"] == [REMOVED] and after[0]["unreset"] == [REMOVED]
    assert pci.removed_chip_ids() == [REMOVED] and srv.isolated_chips == {REMOVED}, "the chip stays isolated"


@pytest.mark.asyncio
async def test_a_failed_mesh_reset_climbs_to_the_next_rung_and_keeps_the_hold(ladder, monkeypatch):  # noqa: F811
    rig = ladder
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)
    srv.isolated_chips = {REMOVED}
    writes = _count_rescans(monkeypatch)
    _launch(monkeypatch, rig, 1, [IPMI_FAILED])
    _verify_returning(monkeypatch, FOK)
    out = await rig["g"]._reset_and_verify_device([str(i) for i in range(32)], lambda m: None)
    assert out is False, "the ladder goes on to its next rung"
    assert rig["launches"] == [MESH] and writes == []
    assert [f["reason"] for f in _withheld(rig)] == ["mesh_reset_failed"]
    assert srv.fsm.state is not srv.ServerState.HEALTHY, "the hold stays"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rc, chunks",
    [(0, [SETTLE, DRIVER_WAIT.replace("found 31", "found 32")]), (1, [SETTLE, DRIVER_WAIT])],
    ids=["rc0", "rc1-after-the-re-power"],
)
async def test_a_mesh_reset_that_re_powered_the_trays_rescans_as_before(ladder, monkeypatch, rc, chunks):  # noqa: F811
    """I81/I78 unchanged: the re-power reached every tray, so the removed chip was reset; the driver-wait
    rescan and the rescan after the reset both run, and a returned chip drops its records."""
    rig = ladder
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)
    srv.isolated_chips = {REMOVED}
    writes = _count_rescans(monkeypatch)
    _launch(monkeypatch, rig, rc, chunks)
    await rig["m"].reset_with_quiesce(MESH, lambda m: None)
    assert len(writes) >= 2, "the driver-wait rescan and the rescan after the reset"
    assert _withheld(rig) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rc, chunks", [(1, [IPMI_FAILED]), (None, [])], ids=["rc1", "launch-failed"])
async def test_a_chip_the_broker_never_removed_keeps_the_bare_rescan(ladder, monkeypatch, rc, chunks):  # noqa: F811
    rig = ladder
    writes = _count_rescans(monkeypatch)
    _launch(monkeypatch, rig, rc, chunks)
    await rig["m"].reset_with_quiesce(MESH, lambda m: None)
    assert len(writes) == 1 and _withheld(rig) == []


@pytest.mark.asyncio
async def test_the_driver_wait_rescan_starts_only_after_the_bmc_re_power_went_out(ladder, monkeypatch):  # noqa: F811
    """A 0x0 fallback ahead of the driver wait (the guard bypassed, tt-smi still counting its settle)
    starts no driver-wait rescan: it is journaled once, and the rescan after the reset is withheld too."""
    rig = ladder
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)
    srv.isolated_chips = {REMOVED}
    writes = _count_rescans(monkeypatch)
    # The fallback text split across two reads still counts.
    half = len(FALLBACK) // 2
    _launch(monkeypatch, rig, 1, [FALLBACK[:half], FALLBACK[half:], SETTLE, DRIVER_WAIT, DRIVER_WAIT])
    await rig["m"].reset_with_quiesce(MESH, lambda m: None)
    assert writes == []
    withheld = _withheld(rig)
    assert [f.get("at") for f in withheld] == ["driver_wait", None]
    assert all(f["reason"] == "mesh_reset_failed" for f in withheld)
    driver_wait = [k for k, _ in rig["events"] if k == "reset_driver_wait_rescan"]
    assert driver_wait == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "healthy, evidence, released",
    [
        (True, FOK, True),
        (False, {"snapshot": {"ok": True}, "fabric": {"ok": False, "detail": "4 links down"}}, False),
        (False, {"snapshot": {"ok": False}}, False),
        (True, F77, False),
        (True, {"snapshot": {"ok": True}}, False),
    ],
    ids=["full-check-passed", "fabric-failed", "enum-arc-failed", "fabric-77", "no-fabric-verdict"],
)
async def test_a_clean_mesh_reset_still_owes_the_full_check_before_release(
    ladder, monkeypatch, healthy, evidence, released  # noqa: F811
):
    """Goals 8/11: the rescan after a re-powered mesh reset releases nothing; the hold lifts only once
    the full check, fabric included, has passed after it, and a failed or unproven check keeps it.
    Driven through the gate's own mesh-reset rung and fold, so nothing here clears the hold."""
    rig = ladder
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)
    srv.isolated_chips = {REMOVED}
    writes = _count_rescans(monkeypatch)
    _launch(monkeypatch, rig, 0, [SETTLE, DRIVER_WAIT.replace("found 31", "found 32")])
    verifies = []

    async def verify(expected, log, run_fabric=True, **_):
        verifies.append(run_fabric)
        return healthy, dict(evidence)

    patch_recovery(monkeypatch, "_verify_device", verify)
    g = rig["g"]
    out = await g._fire_gate_rung(
        "post-job", galaxy.STAGE_SMI_RESET, [str(i) for i in range(32)], 32, lambda m: None, ev=None, beats=None
    )
    srv._fold_rung_outcome(out, lambda m: None)
    assert rig["launches"] == [MESH] and writes, "the re-powered reset rescanned as before"
    assert verifies and all(verifies), "the full check ran, fabric included"
    if released:
        assert out == galaxy.OUTCOME_RECOVERED
        assert srv.fsm.state is srv.ServerState.HEALTHY and FABRIC_DEBT.owed == ""
    else:
        assert srv.fsm.state is not srv.ServerState.HEALTHY, "a hold lifted without a passing full check"
        assert FABRIC_DEBT.owed, "the reset still owes its full check"
        assert not any(
            k == "device_dirty_cleared" and f.get("verified") for k, f in rig["events"]
        ), "released as verified"


@pytest.mark.asyncio
async def test_a_withheld_rescan_does_not_stop_the_ten_minute_ladder(monkeypatch, clear_job_state):
    """Goal 10: a hold left by a failed mesh reset, its removed chip still isolated, runs the full
    ladder at the 10-min bound and then asks for the host rung."""
    why, detail, output, beats, ladder_kind = _KINDS["off-bus"]
    escalations, _alerts, events = _arm_fabric_only_deadline_hold(
        monkeypatch, why=why, detail=detail, fabric_output=output, beats=beats, age_min=BOUND / 60 + 0.05
    )
    monkeypatch.setattr(srv, "_fabric_77_only_hold", lambda: False)
    monkeypatch.setattr(srv, "dead_chips", lambda b: [])
    pci.REMOVED_CHIPS[REMOVED] = dict(REMOVED_REC)

    async def failed_ladder(kind, indices, expected, log, **_):
        escalations.append(kind)
        return OUTCOME_TERMINAL

    monkeypatch.setattr(srv.galaxy_recovery, "escalate", failed_ladder)
    await srv._force_escalate_stuck_hold()
    assert escalations == [ladder_kind]
    assert events.count("host_rung") == 1
