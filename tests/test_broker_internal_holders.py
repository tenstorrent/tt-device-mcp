"""Spec 04 I59: the broker's own device work never reads as a tenant.

Field replay: a chip dropped during the post-reset fabric check; the dead-chip rescue SIGKILLed the
fabric check and a root helper, and the next holder scan, taken while both were still exiting (named
by the driver, fd table gone), read incomplete. The host rung was then denied as "a tenant holds the
device" and the hold sat 8 min to the next ceiling window.
"""

import os

import pytest

from tt_device_mcp import device_holders as dh
from tt_device_mcp.device_holders import DeviceHolder, HolderScan, discount_broker_internal, note_broker_internal


@pytest.fixture(autouse=True)
def _clean_registry():
    dh._clear_broker_internal()
    yield
    dh._clear_broker_internal()


GONE = 2**22 + 12345  # a pid no /proc entry has


def _scan_with_gap(pid):
    scan = HolderScan(holders=[], complete=True, source="driver")
    scan.mark_incomplete(f"pid {pid} named by the driver has no /proc entry here", pid)
    return scan


def test_a_gap_left_by_an_exiting_broker_probe_reads_complete():
    note_broker_internal(GONE)
    out = discount_broker_internal(_scan_with_gap(GONE))
    assert out.complete and out.gaps == [] and out.gap_pids == []


def test_any_other_gap_still_fails_closed():
    note_broker_internal(GONE)
    scan = _scan_with_gap(GONE)
    scan.mark_incomplete("a per-device driver pids file was unreadable")
    scan.mark_incomplete(f"pid {GONE + 1} named by the driver has no /proc entry here", GONE + 1)
    out = discount_broker_internal(scan)
    assert not out.complete and len(out.gaps) == 2


def test_an_unregistered_gap_is_untouched():
    scan = _scan_with_gap(GONE)
    assert discount_broker_internal(scan) is scan and not scan.complete


def test_a_registered_holder_is_dropped_but_a_scoped_one_never_is():
    me = os.getpid()
    note_broker_internal(me)
    plain = DeviceHolder(pid=me, uid=0)
    scoped = DeviceHolder(pid=me, uid=0, scoped=True)
    out = discount_broker_internal(HolderScan(holders=[plain, scoped], complete=True, source="driver"))
    assert len(out.holders) == 1 and out.holders[0].scoped


def test_a_reused_pid_with_a_new_starttime_is_not_internal(monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(dh, "_read_proc_starttime", lambda pid: "first")
    monkeypatch.setattr(dh, "_read_proc_pgid", lambda pid: pid)
    note_broker_internal(me)
    assert dh.is_broker_internal(me)
    monkeypatch.setattr(dh, "_read_proc_starttime", lambda pid: "other")
    assert not dh.is_broker_internal(me)


def test_registrations_expire():
    note_broker_internal(GONE, now=0.0)
    assert dh.is_broker_internal(GONE, now=1.0)
    assert not dh.is_broker_internal(GONE, now=dh.BROKER_INTERNAL_TTL_SEC + 1)


def test_a_holder_without_a_scope_field_is_discounted_not_crashed():
    """Duck-typed holders (no `scoped`) count as unscoped: a registered pid is dropped, no AttributeError."""
    note_broker_internal(GONE)
    h = type("H", (), {"uid": 0, "pid": GONE})()
    out = discount_broker_internal(HolderScan(holders=[h], complete=True, source="driver"))
    assert out.holders == []
