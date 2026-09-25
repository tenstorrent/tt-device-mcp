# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""The eth-heartbeat rung arms itself, or says so.

The rung it replaces was opt-in and nobody ever opted in: 25 SKIPPED and 0 verdicts over weeks,
invisible because "not armed" and "broken" produce identical silence. These pin the two halves of the
fix — the box times its own read and arms on a fast answer, and a rung that stays off is stated at
WARNING rather than inferred from an absence of verdicts.
"""

import pytest

import tt_device_mcp.server as srv
from tt_device_mcp.health.monitors import eth


@pytest.fixture(autouse=True)
def _reset_arming(monkeypatch):
    monkeypatch.setattr(srv, "eth_check_armed", False)
    monkeypatch.setattr(srv, "eth_check_disarm_reason", "startup self-test has not run yet")
    srv.health_monitor._skip_events_journaled.clear()
    yield


@pytest.mark.asyncio
async def test_a_fast_clean_read_arms_the_rung(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "exit 0")
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is True
    assert srv.eth_check_disarm_reason == ""


@pytest.mark.asyncio
async def test_a_frozen_verdict_still_counts_as_armed(monkeypatch):
    # rc 1 is the detector WORKING and finding something. Refusing to arm on it would disable the
    # rung on precisely the box that needs it.
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "exit 1")
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is True


@pytest.mark.asyncio
async def test_a_cannot_check_read_leaves_the_rung_off_with_a_reason(monkeypatch):
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "echo no ttexalens; exit 77")
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is False
    assert "could not check" in srv.eth_check_disarm_reason


@pytest.mark.asyncio
async def test_a_slow_read_never_arms(monkeypatch):
    # The dangerous case: past its timeout the gate reads a frozen core and HOLDS the box, so a read
    # too slow to distinguish from a freeze must not be trusted to deliver verdicts.
    monkeypatch.setattr(srv, "ETH_CHECK_SELFTEST_BUDGET_SEC", 0.3)
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "sleep 5")
    await srv.selftest_eth_heartbeat()
    assert srv.eth_check_armed is False
    assert "hung past" in srv.eth_check_disarm_reason


@pytest.mark.asyncio
async def test_a_disarmed_rung_skips_instead_of_delivering_a_verdict(monkeypatch):
    # Skip, never False: an untimed reader that runs slow is indistinguishable from a frozen core,
    # and that verdict holds the box. Arming gates BOTH paths (see the override test below).
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", raising=False)
    monkeypatch.setattr(eth, "resolve_python", lambda **k: ("/usr/bin/python3", "/tree"))
    monkeypatch.setattr(eth, "_probe_path", lambda: "/probe.py")

    assert eth.build() is None, "an unarmed host must have nothing to check with"
    ok, detail = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is None
    assert "not armed" in detail


@pytest.mark.asyncio
async def test_a_disarmed_override_skips_too_instead_of_delivering_a_verdict(monkeypatch):
    """The self-test times the OVERRIDE as well (test_a_slow_read_never_arms runs one) — so its
    disarm must gate the override at verdict time exactly as the pre-split inline check did.
    Ungated, a slow operator command runs untimed at every gate pass, where its hang maps to a
    frozen-core HOLD and strands a healthy mesh on a reader problem — the exact outcome the
    self-test exists to prevent. (The self-test itself still measures the override: it arms
    provisionally around its one run, see selftest_eth_heartbeat.)"""
    monkeypatch.setenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", "exit 3")  # would read frozen if run
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", raising=False)

    assert eth.build() is None, "a disarmed override must have nothing to check with"
    ok, detail = await srv.health_monitor.verify_eth_heartbeat()
    assert ok is None, "a disarmed reader skips; only an armed one may deliver a HOLD verdict"
    assert "not armed" in detail


def test_an_armed_rung_delivers_its_verdict(monkeypatch):
    # Armed, and everything else resolvable: the rung has a runnable command to deliver a verdict
    # with. Unarmed it had none, which is what makes the skip above a skip and not a False.
    monkeypatch.delenv("TT_DEVICE_MCP_ETH_HEARTBEAT_CMD", raising=False)
    monkeypatch.setattr(eth, "resolve_python", lambda **k: ("/usr/bin/python3", "/tree"))
    monkeypatch.setattr(eth, "_probe_path", lambda: "/probe.py")
    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "1")

    built = eth.build()
    assert built is not None
    argv, _env = built
    assert "/probe.py" in argv


def test_the_armed_flag_is_not_inherited_from_a_stale_environment(monkeypatch):
    # The reader must not be armable by accident. Arming is one explicit flag, and the pre-rename
    # spelling stays honoured so a host armed by hand does not silently disarm on upgrade.
    monkeypatch.delenv("TTDEV_ETH_CHECK_ARMED", raising=False)
    monkeypatch.delenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", raising=False)
    assert eth.armed() is False

    monkeypatch.setenv("TTDEV_ETH_CHECK_ALLOW_KNOWN_BROKEN", "1")
    assert eth.armed() is True, "the pre-rename spelling must keep an armed host armed"

    monkeypatch.setenv("TTDEV_ETH_CHECK_ARMED", "0")
    assert eth.armed() is False, "the current spelling wins over the legacy one"


def test_a_rung_that_is_off_is_stated_loudly(monkeypatch):
    # The whole point: an off rung is readable in the journal, not inferred from silence.
    said = []

    class _Logger:
        def info(self, msg):
            said.append(("info", msg))

        def warning(self, msg):
            said.append(("warning", msg))

        def error(self, msg):
            said.append(("error", msg))

    monkeypatch.setattr(srv, "logger", _Logger())
    monkeypatch.setenv("TT_DEVICE_MCP_PREJOB_DISPATCH", "0")
    srv.log_rung_inventory()
    assert any(
        lvl == "info" and "RUNG INVENTORY" in m for lvl, m in said
    ), "every rung's state must be stated, on or off"
    assert any(
        lvl == "warning" and "RUNG OFF pre-job-dispatch-probe" in m for lvl, m in said
    ), "a rung that is off must be a WARNING naming it, not a silence"
