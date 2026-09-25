# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone

from tt_device_mcp.health.core import HealthState, Observation, Verdict


def _obs(name, verdict):
    return Observation(monitor=name, verdict=verdict, detail="", evidence={}, phase="pre-job")


def test_verdict_has_skipped():
    assert Verdict.SKIPPED.value == "skipped"


def test_healthstate_any_unhealthy_is_unhealthy():
    st = HealthState(
        phase="pre-job",
        at=datetime.now(timezone.utc),
        expected=32,
        observations=(_obs("heartbeat", Verdict.HEALTHY), _obs("fabric", Verdict.UNHEALTHY)),
    )
    assert not st.healthy


def test_healthstate_skipped_never_heals():
    st = HealthState(
        phase="pre-job", at=datetime.now(timezone.utc), expected=32, observations=(_obs("fabric", Verdict.SKIPPED),)
    )
    assert st.healthy  # skipped is not unhealthy...
    assert st.of("fabric").verdict is Verdict.SKIPPED  # ...but is visible as skipped


def test_healthstate_of_missing_monitor_is_none():
    st = HealthState(phase="pre-job", at=datetime.now(timezone.utc), expected=1, observations=())
    assert st.of("eth") is None


def test_healthstate_typed_accessors_default_absent_to_not_frozen_not_ran():
    st = HealthState(phase="pre-job", at=datetime.now(timezone.utc), expected=1, observations=())
    assert st.fabric_ok is None
    assert st.fabric_ran is False
    assert st.eth_frozen is False
    assert st.frozen_chips == 0
    assert st.detail("fabric") == ""


def test_healthstate_fabric_ok_true_false_and_skipped_stay_distinct_from_never_ran():
    healthy = HealthState(
        phase="p", at=datetime.now(timezone.utc), expected=1, observations=(_obs("fabric", Verdict.HEALTHY),)
    )
    unhealthy = HealthState(
        phase="p", at=datetime.now(timezone.utc), expected=1, observations=(_obs("fabric", Verdict.UNHEALTHY),)
    )
    skipped = HealthState(
        phase="p", at=datetime.now(timezone.utc), expected=1, observations=(_obs("fabric", Verdict.SKIPPED),)
    )
    assert healthy.fabric_ok is True and healthy.fabric_ran is True
    assert unhealthy.fabric_ok is False and unhealthy.fabric_ran is True
    # A 77 (ran, no verdict) and "never reached the pass" both read fabric_ok is None, but only
    # fabric_ran tells them apart — that distinction is _fabric_ran_but_unverified's whole point.
    assert skipped.fabric_ok is None and skipped.fabric_ran is True


def test_healthstate_eth_frozen_only_true_on_a_confirmed_freeze():
    frozen = HealthState(
        phase="p", at=datetime.now(timezone.utc), expected=1, observations=(_obs("eth_heartbeat", Verdict.UNHEALTHY),)
    )
    skipped = HealthState(
        phase="p", at=datetime.now(timezone.utc), expected=1, observations=(_obs("eth_heartbeat", Verdict.SKIPPED),)
    )
    assert frozen.eth_frozen is True
    assert skipped.eth_frozen is False


def test_healthstate_frozen_chips_reads_the_heartbeat_stalled_list():
    obs = Observation(
        monitor="heartbeat", verdict=Verdict.UNHEALTHY, detail="frozen", evidence={"stalled": ["3", "7"]}, phase="p"
    )
    st = HealthState(phase="p", at=datetime.now(timezone.utc), expected=8, observations=(obs,))
    assert st.frozen_chips == 2


def test_healthstate_as_evidence_relabels_pci_to_snapshot():
    """The tt-smi probe is "pci" in the Observation vocabulary but the durable journal and the
    test suite's fakes still expect the pre-extraction name "snapshot" — as_evidence() must keep
    emitting it, or the journal's vocabulary and every {"snapshot": ...} fake break silently."""
    obs = Observation(monitor="pci", verdict=Verdict.HEALTHY, detail="32 chips", evidence={}, phase="p")
    st = HealthState(phase="p", at=datetime.now(timezone.utc), expected=32, observations=(obs,))
    ev = st.as_evidence()
    assert "pci" not in ev
    assert ev["snapshot"] == {"ok": True, "detail": "32 chips"}


def test_healthstate_as_evidence_matches_the_legacy_verify_device_shape():
    observations = (
        Observation(
            monitor="heartbeat", verdict=Verdict.HEALTHY, detail="all advancing", evidence={"chips": 32}, phase="p"
        ),
        Observation(monitor="pci", verdict=Verdict.HEALTHY, detail="32 chips", evidence={}, phase="p"),
        Observation(monitor="eth_heartbeat", verdict=Verdict.SKIPPED, detail="skipped", evidence={}, phase="p"),
        Observation(monitor="fabric", verdict=Verdict.UNHEALTHY, detail="link down", evidence={}, phase="p"),
    )
    st = HealthState(phase="p", at=datetime.now(timezone.utc), expected=32, observations=observations)
    assert st.as_evidence() == {
        "heartbeat": {"verdict": "healthy", "detail": "all advancing", "chips": 32},
        "snapshot": {"ok": True, "detail": "32 chips"},
        "eth_heartbeat": {"ok": None, "detail": "skipped"},
        "fabric": {"ok": False, "detail": "link down"},
    }


def test_healthstate_from_evidence_round_trips_through_as_evidence():
    """from_evidence() is the read side of as_evidence() — a state that goes dict -> state ->
    dict must come back unchanged, or the gate's typed accessors would disagree with the very
    evidence dict they were reconstructed from."""
    observations = (
        Observation(
            monitor="heartbeat",
            verdict=Verdict.UNHEALTHY,
            detail="2 frozen",
            evidence={"stalled": ["3", "7"]},
            phase="p",
        ),
        Observation(monitor="pci", verdict=Verdict.HEALTHY, detail="32 chips", evidence={}, phase="p"),
        Observation(monitor="eth_heartbeat", verdict=Verdict.SKIPPED, detail="skipped", evidence={}, phase="p"),
        Observation(monitor="fabric", verdict=Verdict.UNHEALTHY, detail="link down", evidence={}, phase="p"),
    )
    original = HealthState(phase="p", at=datetime.now(timezone.utc), expected=32, observations=observations)
    rebuilt = HealthState.from_evidence(original.as_evidence(), phase="p", expected=32)
    assert rebuilt.as_evidence() == original.as_evidence()
    assert rebuilt.fabric_ok is original.fabric_ok
    assert rebuilt.eth_frozen is original.eth_frozen
    assert rebuilt.frozen_chips == original.frozen_chips


def test_healthstate_from_evidence_matches_the_gates_old_dict_reads():
    """Each partial dict a ``patch_recovery("_verify_device", ...)`` test fake hands back — most
    supply only the ONE probe their scenario cares about — must read through the typed accessors
    exactly the way the gate's old raw dict lookups read them:
    ``(evidence.get("fabric") or {}).get("ok")``, ``(evidence.get("eth_heartbeat") or
    {}).get("ok") is False``, and ``len((evidence.get("heartbeat") or {}).get("stalled") or [])``.
    """
    fabric_skipped = HealthState.from_evidence({"fabric": {"ok": None, "detail": "77"}}, phase="p")
    assert fabric_skipped.fabric_ok is None

    fabric_missing = HealthState.from_evidence({"snapshot": {"ok": True}}, phase="p")
    assert fabric_missing.fabric_ok is None  # never ran this pass, same as a 77

    eth_frozen = HealthState.from_evidence({"eth_heartbeat": {"ok": False, "detail": "frozen"}}, phase="p")
    assert eth_frozen.eth_frozen is True

    eth_skipped = HealthState.from_evidence({"eth_heartbeat": {"ok": None, "detail": "skip"}}, phase="p")
    assert eth_skipped.eth_frozen is False

    eth_missing = HealthState.from_evidence({}, phase="p")
    assert eth_missing.eth_frozen is False

    mass_frozen = HealthState.from_evidence(
        {"heartbeat": {"verdict": "unhealthy", "detail": "all frozen", "stalled": [str(i) for i in range(32)]}},
        phase="p",
    )
    assert mass_frozen.frozen_chips == 32

    few_frozen = HealthState.from_evidence(
        {"heartbeat": {"verdict": "unhealthy", "detail": "3 frozen", "stalled": ["3", "8", "17"]}}, phase="p"
    )
    assert few_frozen.frozen_chips == 3

    no_heartbeat = HealthState.from_evidence({"snapshot": {"ok": False}}, phase="p")
    assert no_heartbeat.frozen_chips == 0


def test_healthstate_from_evidence_carries_the_callers_real_phase():
    """as_evidence() carries no ``phase`` key — from_evidence() must take it from the caller (the
    gate's own real 'pre-job'/'post-job'/'startup'), not default it to a placeholder."""
    st = HealthState.from_evidence({"snapshot": {"ok": True}}, phase="post-job", expected=32)
    assert st.phase == "post-job"
    assert st.expected == 32
