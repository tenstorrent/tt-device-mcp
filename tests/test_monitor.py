# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the HealthMonitor aggregate: the update() glue that composes the individual probes
into a HealthState and stores it as the readings blackboard."""

import pytest

from tt_device_mcp.constants import ETH_POST_JOB_TIMEOUT_SEC
from tt_device_mcp.health.core import Verdict
from tt_device_mcp.health.monitor import HealthMonitor


@pytest.mark.asyncio
async def test_update_overwrites_readings(monkeypatch, health_deps):
    m = HealthMonitor(health_deps)

    async def fake_all_healthy(*a, **k):
        return True, "ok"

    # update() reaches the tt-smi snapshot probe through this internal seam; faking it here keeps
    # the test off real hardware while still exercising update()'s own composition/storage logic.
    monkeypatch.setattr(m, "_verify_device", fake_all_healthy)
    st = await m.update("pre-job", run_fabric=False)
    assert st is m.status()
    assert st.phase == "pre-job"


@pytest.mark.asyncio
async def test_update_short_circuits_on_an_unhealthy_snapshot(monkeypatch, health_deps):
    """A failed tt-smi snapshot must not run the eth/fabric probes — the same gentlest-first
    short-circuiting server._verify_device has always done, since those probes actively push a
    frozen chip off the bus."""
    m = HealthMonitor(health_deps)

    async def fake_unhealthy(*a, **k):
        return False, "chip(s) [0] returned no board_id/telemetry (ARC wedged)"

    calls = {"eth": 0, "fabric": 0}

    async def fake_eth(*a, **k):
        calls["eth"] += 1
        return True, "advancing"

    async def fake_fabric(*a, **k):
        calls["fabric"] += 1
        return True, "healthy"

    monkeypatch.setattr(m, "_verify_device", fake_unhealthy)
    monkeypatch.setattr(m, "verify_eth_heartbeat", fake_eth)
    monkeypatch.setattr(m, "verify_fabric_health", fake_fabric)

    st = await m.update("post-job", run_fabric=True)

    assert st.healthy is False
    assert calls == {"eth": 0, "fabric": 0}
    pci_obs = st.of("pci")
    assert pci_obs is not None and pci_obs.verdict is Verdict.UNHEALTHY


@pytest.mark.asyncio
async def test_status_never_blocks_before_the_first_update(health_deps):
    m = HealthMonitor(health_deps)
    assert m.status() is None


@pytest.mark.asyncio
async def test_update_derives_present_from_dev_enumeration_not_heartbeat(monkeypatch, health_deps):
    """Regression: on a host whose driver exposes no ARC heartbeat at all
    (heartbeat_supported() permanently False), read_heartbeats() returns {} unconditionally —
    it is not a signal about how many chips are actually on the bus. update() must derive its
    present-chip count from the SAME /dev/tenstorrent enumeration the gate uses (the injected
    present_chip_indices dep), not from the heartbeat read, or a heartbeat-unsupported host with
    3 real chips present and no baseline file yet computes expected=0 and its enumeration-count
    check (verify_device_health's ``len(devs) < expected_count``) is permanently neutered."""
    m = HealthMonitor(health_deps)
    monkeypatch.setattr(health_deps, "heartbeat_supported", lambda: False)
    # 3 chips present on the bus; no baseline file exists yet (isolate_device_state points
    # HEALTH_DIR at a fresh empty tmp dir per test).
    monkeypatch.setattr(m, "_present_chip_indices", lambda: ["0", "1", "2"])

    seen_expected = {}

    async def fake_verify_device(expected):
        seen_expected["value"] = expected
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", fake_verify_device)

    st = await m.update("pre-job", run_fabric=False)

    assert seen_expected["value"] == 3, (
        "update() computed expected from an empty heartbeat read (0) instead of the real "
        "3-chip /dev/tenstorrent enumeration"
    )
    assert st.expected == 3


@pytest.mark.asyncio
async def test_update_accepts_caller_supplied_indices_and_expected(monkeypatch, health_deps):
    """A caller that already read the mesh this pass (the gate reads indices/expected before it
    ever reaches a probe) must be able to hand both in — update() re-reading present_chip_indices()
    and re-deriving expected() on its own can disagree with the caller's own read, and expected()
    WRITES the chip-baseline file, so a second read can ratchet the high-water mark a second time
    in the same pass."""
    m = HealthMonitor(health_deps)

    def boom():
        raise AssertionError("update() re-read present_chip_indices() despite an explicit expected=")

    monkeypatch.setattr(m, "_present_chip_indices", boom)
    monkeypatch.setattr(
        m,
        "expected",
        lambda present: (_ for _ in ()).throw(
            AssertionError("update() re-derived expected() despite an explicit expected=")
        ),
    )

    seen_expected = {}

    async def fake_verify_device(expected):
        seen_expected["value"] = expected
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", fake_verify_device)

    st = await m.update("pre-job", run_fabric=False, expected=7)

    assert seen_expected["value"] == 7
    assert st.expected == 7


@pytest.mark.asyncio
async def test_update_routes_heartbeat_through_the_injected_deps(monkeypatch, health_deps):
    """Recovery._verify_device has always called self.deps.heartbeat_supported()/heartbeat_verdict
    — the seam ~50 tests drive via monkeypatch.setattr(srv, "heartbeat_supported"/"heartbeat_verdict",
    ...). update() must go through the SAME deps, not the module-level heartbeat_supported/
    heartbeat_verdict — those are two different name bindings, so a patch to one is invisible to
    code reading the other. Proof: patch health_deps (not the monitor module) to force a heartbeat
    verdict update() could only see by reading self._deps."""
    m = HealthMonitor(health_deps)
    monkeypatch.setattr(health_deps, "heartbeat_supported", lambda: True)

    def fake_verdict(expected):
        return Verdict.UNHEALTHY, "forced unhealthy via the injected deps", {"stalled": ["0", "1"]}

    monkeypatch.setattr(health_deps, "heartbeat_verdict", fake_verdict)

    pci_calls = {"n": 0}

    async def fake_pci(expected):
        pci_calls["n"] += 1
        return True, "ok"

    monkeypatch.setattr(m, "_verify_device", fake_pci)

    st = await m.update("pre-job", run_fabric=False)

    assert st.healthy is False, "the deps-injected UNHEALTHY heartbeat verdict never reached update()"
    assert pci_calls["n"] == 0, "an UNHEALTHY heartbeat must short-circuit before the pci probe runs"
    assert st.frozen_chips == 2


@pytest.mark.asyncio
async def test_update_never_runs_the_fabric_pass_after_a_frozen_eth_core(monkeypatch, health_deps):
    """The traffic pass is what converts a frozen-but-present chip into an off-the-bus
    0xFFFFFFFF drop, so update() must never reach it once the passive eth-heartbeat read comes
    back frozen — the same invariant Recovery._verify_device has always enforced."""
    m = HealthMonitor(health_deps)

    async def healthy_pci(expected):
        return True, "ok"

    async def frozen_eth():
        return False, "a frozen active-eth core"

    calls = {"fabric": 0}

    async def fabric(*a, **k):
        calls["fabric"] += 1
        return True, "links healthy"

    monkeypatch.setattr(m, "_verify_device", healthy_pci)
    monkeypatch.setattr(m, "verify_eth_heartbeat", frozen_eth)
    monkeypatch.setattr(m, "verify_fabric_health", fabric)

    st = await m.update("pre-job", run_fabric=True)

    assert calls["fabric"] == 0, "ran the traffic pass across a frozen eth core — the iatrogenic drop"
    assert st.eth_frozen is True
    assert st.fabric_ran is False
    assert st.healthy is False


@pytest.mark.asyncio
async def test_update_still_runs_the_fabric_pass_when_eth_is_skipped(monkeypatch, health_deps):
    """An eth-heartbeat SKIP (unconfigured/not-runnable) learned nothing about the eth cores, so
    falling through to the definitive traffic pass is correct — only a confirmed FROZEN read must
    hold it back."""
    m = HealthMonitor(health_deps)

    async def healthy_pci(expected):
        return True, "ok"

    async def skipped_eth():
        return None, "skipped (not configured)"

    calls = {"fabric": 0}

    async def fabric(*a, **k):
        calls["fabric"] += 1
        return True, "links healthy"

    monkeypatch.setattr(m, "_verify_device", healthy_pci)
    monkeypatch.setattr(m, "verify_eth_heartbeat", skipped_eth)
    monkeypatch.setattr(m, "verify_fabric_health", fabric)

    st = await m.update("pre-job", run_fabric=True)

    assert calls["fabric"] == 1, "an eth SKIP must not suppress the definitive traffic pass"
    assert st.fabric_ran is True
    assert st.eth_frozen is False


def _run_eth_monitor(monkeypatch, health_deps, eth):
    """A HealthMonitor with a passing snapshot, an eth read returning `eth`, and a passing fabric
    pass, all counted. Spec 03 I30."""
    m = HealthMonitor(health_deps)
    calls = {"eth": 0, "eth_timeout": None, "fabric": 0}

    async def healthy_pci(expected):
        return True, "ok"

    async def eth_read(timeout_sec=60.0):
        calls["eth"] += 1
        calls["eth_timeout"] = timeout_sec
        return eth

    async def fabric(*a, **k):
        calls["fabric"] += 1
        return True, "links healthy"

    monkeypatch.setattr(m, "_verify_device", healthy_pci)
    monkeypatch.setattr(m, "verify_eth_heartbeat", eth_read)
    monkeypatch.setattr(m, "verify_fabric_health", fabric)
    return m, calls


@pytest.mark.asyncio
async def test_update_run_eth_reads_eth_with_the_post_job_bound_and_skips_fabric(monkeypatch, health_deps):
    """run_eth alone is the clean post-job read: the passive eth read, bounded to
    ETH_POST_JOB_TIMEOUT_SEC rather than the fabric path's 60s, and no traffic pass after it."""
    m, calls = _run_eth_monitor(monkeypatch, health_deps, (True, "all advancing"))

    st = await m.update("post-job", run_fabric=False, run_eth=True)

    assert calls["eth"] == 1
    assert calls["eth_timeout"] == ETH_POST_JOB_TIMEOUT_SEC
    assert calls["fabric"] == 0, "a clean exit whose eth cores all advance paid for a traffic pass"
    assert st.fabric_ran is False
    assert st.healthy is True


@pytest.mark.asyncio
async def test_update_run_eth_runs_fabric_when_eth_reaches_no_verdict(monkeypatch, health_deps):
    """A run_eth read that reaches no verdict (its own timeout, a crash) runs the traffic pass in
    the same update(): on an armed host that read is the stuck-read shape a fabric failure follows."""
    m, calls = _run_eth_monitor(monkeypatch, health_deps, (None, "eth probe timed out after 9s"))

    st = await m.update("post-job", run_fabric=False, run_eth=True)

    assert calls["fabric"] == 1, "a stuck eth read on a clean exit let the mesh through unchecked"
    assert st.fabric_ran is True
    assert st.eth_frozen is False


@pytest.mark.asyncio
async def test_update_run_eth_never_runs_fabric_after_a_frozen_eth_core(monkeypatch, health_deps):
    """A frozen run_eth read stops the pass like any frozen read: no traffic pass across it."""
    m, calls = _run_eth_monitor(monkeypatch, health_deps, (False, "a frozen active-eth core"))

    st = await m.update("post-job", run_fabric=False, run_eth=True)

    assert calls["fabric"] == 0, "ran the traffic pass across a frozen eth core"
    assert st.eth_frozen is True
    assert st.healthy is False


@pytest.mark.asyncio
async def test_update_without_run_eth_or_fabric_reads_no_eth(monkeypatch, health_deps):
    """The default stays the light pass: no eth read unless a caller asks for it."""
    m, calls = _run_eth_monitor(monkeypatch, health_deps, (True, "all advancing"))

    await m.update("post-job", run_fabric=False)

    assert calls["eth"] == 0
    assert calls["fabric"] == 0


@pytest.mark.asyncio
async def test_update_logs_and_sets_device_op_detail_per_probe(monkeypatch, health_deps):
    """update() owns the per-probe job-log lines and set_device_op_detail strings
    Recovery._verify_device used to write directly — losing either blanks the job log and the
    queue's "what's it doing" detail for every caller now that _verify_device just delegates here."""
    m = HealthMonitor(health_deps)
    monkeypatch.setattr(health_deps, "heartbeat_supported", lambda: True)
    monkeypatch.setattr(health_deps, "heartbeat_verdict", lambda expected: (Verdict.HEALTHY, "all advancing", {}))

    async def healthy_pci(expected):
        return True, "32 chips"

    async def healthy_eth():
        return True, "advancing"

    async def healthy_fabric():
        return True, "links healthy"

    monkeypatch.setattr(m, "_verify_device", healthy_pci)
    monkeypatch.setattr(m, "verify_eth_heartbeat", healthy_eth)
    monkeypatch.setattr(m, "verify_fabric_health", healthy_fabric)

    details = []
    monkeypatch.setattr(health_deps, "set_device_op_detail", lambda d: details.append(d))
    lines = []

    st = await m.update("pre-job", run_fabric=True, log=lines.append)

    assert details == [
        "health check: host PCI (driver binding, BARs)",
        "health check: chip heartbeat",
        "health check: chip enumeration (tt-smi)",
        "health check: eth-core heartbeat (passive)",
        "health check: fabric traffic pass across all links (~45s)",
    ]
    # host-pci SKIPs here rather than passing: conftest sandboxes pci.PCI_DEVICES_DIR to an empty
    # dir, so the bus shows no Tenstorrent function and this probe has no opinion — the same shape
    # a device-less runner sees. A test that wants it to pass stages its own bus.
    assert any(line.startswith("host-pci: SKIPPED") for line in lines)
    assert any(line.startswith("heartbeat: HEALTHY") for line in lines)
    assert any(line.startswith("snapshot: OK") for line in lines)
    assert any(line.startswith("eth-heartbeat: OK") for line in lines)
    assert any(line.startswith("fabric: OK") for line in lines)
    assert st.healthy is True
