# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the device-holder reset gate (mocked holder enumeration)."""

from tt_device_mcp.device_holders import (
    DeviceHolder,
    HolderScan,
    evaluate_reset_gate,
)

CALLER = 1000
OTHER = 2000


def scan(holder_uids, complete=True):
    holders = [DeviceHolder(pid=1000 + i, uid=uid) for i, uid in enumerate(holder_uids)]
    return HolderScan(holders=holders, complete=complete)


class TestForeignHolders:
    def test_foreign_holders_filters_caller(self):
        s = scan([CALLER, OTHER, CALLER, 3000])
        foreign = s.foreign_holders(CALLER)
        assert {h.uid for h in foreign} == {OTHER, 3000}

    def test_no_foreign_when_all_caller(self):
        s = scan([CALLER, CALLER])
        assert s.foreign_holders(CALLER) == []

    def test_ignores_system_holders(self):
        # root (0) + a system daemon (e.g. tt_telemetry_server, uid 0/<1000) are
        # infrastructure, not tenants — excluded so reset isn't permanently blocked.
        s = scan([0, 117, CALLER])
        assert s.foreign_holders(CALLER) == []


class TestResetGateSystemHolders:
    def test_allows_over_system_holder(self):
        d = evaluate_reset_gate(CALLER, scan([0, CALLER]))
        assert d.allowed is True
        assert d.foreign_holders == []


class TestResetGateAllow:
    def test_allows_when_idle(self):
        d = evaluate_reset_gate(CALLER, scan([]))
        assert d.allowed is True
        assert d.foreign_holders == []

    def test_allows_when_only_own_holders(self):
        # "reset my own wedge" case
        d = evaluate_reset_gate(CALLER, scan([CALLER, CALLER]))
        assert d.allowed is True
        assert d.foreign_holders == []


class TestResetGateDeny:
    def test_denies_on_foreign_holder(self):
        d = evaluate_reset_gate(CALLER, scan([CALLER, OTHER]))
        assert d.allowed is False
        assert [h.uid for h in d.foreign_holders] == [OTHER]
        assert "foreign" in d.reason

    def test_denies_on_multiple_foreign(self):
        d = evaluate_reset_gate(CALLER, scan([OTHER, 3000]))
        assert d.allowed is False
        assert len(d.foreign_holders) == 2


class TestResetGateForce:
    def test_force_overrides_foreign(self):
        d = evaluate_reset_gate(CALLER, scan([OTHER]), force=True)
        assert d.allowed is True
        # Force still surfaces the foreign holders so they can be logged
        assert [h.uid for h in d.foreign_holders] == [OTHER]
        assert "forced" in d.reason

    def test_force_when_idle(self):
        d = evaluate_reset_gate(CALLER, scan([]), force=True)
        assert d.allowed is True
        assert d.foreign_holders == []


class TestResetGateAnonymous:
    # caller_uid=None is an HTTP reset with no SO_PEERCRED identity (privsep host).
    # It owns no holder, so every real tenant is foreign; the same fail-closed rules apply.
    def test_anonymous_denied_over_a_tenant_holder(self):
        d = evaluate_reset_gate(None, scan([OTHER]))
        assert d.allowed is False
        assert [h.uid for h in d.foreign_holders] == [OTHER]

    def test_anonymous_denied_when_scan_incomplete(self):
        d = evaluate_reset_gate(None, scan([], complete=False))
        assert d.allowed is False
        assert "incomplete" in d.reason

    def test_anonymous_allowed_on_a_provably_idle_device(self):
        # A foreign holder CAN be ruled out (complete scan, no tenants) — nothing to protect.
        d = evaluate_reset_gate(None, scan([]))
        assert d.allowed is True

    def test_anonymous_ignores_system_holders(self):
        # root/daemons survive a reset and aren't tenants, so they don't block an anonymous reset.
        d = evaluate_reset_gate(None, scan([0, 117]))
        assert d.allowed is True

    def test_anonymous_force_overrides(self):
        d = evaluate_reset_gate(None, scan([OTHER]), force=True)
        assert d.allowed is True
        assert [h.uid for h in d.foreign_holders] == [OTHER]


class TestResetGateIncompleteScan:
    def test_incomplete_scan_fails_closed_when_no_visible_foreign(self):
        # Degraded visibility: no foreign holder was seen but a cross-uid
        # process was unreadable, so one may be hiding. Fail closed — deny.
        d = evaluate_reset_gate(CALLER, scan([CALLER], complete=False))
        assert d.allowed is False
        assert d.scan_complete is False
        assert "incomplete" in d.reason

    def test_incomplete_scan_denies_on_visible_foreign(self):
        d = evaluate_reset_gate(CALLER, scan([OTHER], complete=False))
        assert d.allowed is False
        assert d.scan_complete is False

    def test_force_overrides_incomplete_scan(self):
        # Force is the escape hatch for the blind spot, and it must say so.
        d = evaluate_reset_gate(CALLER, scan([CALLER], complete=False), force=True)
        assert d.allowed is True
        assert d.scan_complete is False
        assert "incomplete" in d.reason
