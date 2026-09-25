# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared health vocabulary: verdicts, one probe's observation, one pass's readings."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional


class Verdict(str, Enum):
    """A health check answers exactly one question: is the device usable?

    There is no third state. A check that crashed, hung, or returned garbage is
    reporting UNHEALTHY, not reporting nothing — on this hardware a tool blows up
    precisely because the chip underneath it is wedged. Treating a broken check as
    "inconclusive, do nothing" leaves a dead chip in service for the next tenant,
    which is the failure this gate exists to prevent.

    The cost of being wrong in each direction is not symmetric. A needless reset
    of an idle device costs ~60s and nothing else — the gate never resets while a
    tenant holds the device. A missed wedge costs everyone on the box. So when a
    check fails, reset. The safety comes from how the reset is performed (single,
    serialized, uninterruptible, on a quiesced bus), never from declining to run it.
    """

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    # Only an unconfigured probe or an explicit exit-77 is a skip; a crash or hang
    # is UNHEALTHY. A check that cannot fail is not a check.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Observation:
    monitor: str
    verdict: Verdict
    detail: str
    evidence: Mapping[str, Any]
    phase: str


# The tt-smi enumeration probe is labeled "pci" as an Observation (monitor.py's own probe
# vocabulary) but has always journaled and been faked in tests as "snapshot" — the name predates
# the health/ extraction. as_evidence() must keep emitting the old name, or the durable journal's
# vocabulary and the 30+ test fakes that construct {"snapshot": ...} dicts both break silently.
_LEGACY_LABELS = {"pci": "snapshot"}
_LEGACY_LABELS_REVERSE = {v: k for k, v in _LEGACY_LABELS.items()}


@dataclass(frozen=True)
class HealthState:
    phase: str
    at: datetime
    expected: int
    observations: tuple = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return not any(o.verdict is Verdict.UNHEALTHY for o in self.observations)

    def of(self, monitor: str) -> "Optional[Observation]":
        for o in self.observations:
            if o.monitor == monitor:
                return o
        return None

    def detail(self, monitor: str) -> str:
        """The named probe's own detail string, or "" if it never ran this pass."""
        o = self.of(monitor)
        return o.detail if o is not None else ""

    @property
    def fabric_ok(self) -> Optional[bool]:
        """The fabric traffic pass's tri-state verdict: True/False on a real one, None both when
        it never ran and when it ran but reached no verdict (a 77) — see ``fabric_ran`` for
        telling those two apart."""
        o = self.of("fabric")
        if o is None:
            return None
        return True if o.verdict is Verdict.HEALTHY else False if o.verdict is Verdict.UNHEALTHY else None

    @property
    def fabric_ran(self) -> bool:
        """Whether the fabric traffic pass ran at all this pass, independent of its verdict — a
        77 (ran, no verdict) and "never reached it" both read ``fabric_ok is None`` and must not
        collapse to the same thing."""
        return self.of("fabric") is not None

    @property
    def eth_frozen(self) -> bool:
        """Whether the passive eth-core heartbeat read found a frozen core. Unconfigured/skipped
        reads False here, same as a probe that never ran — only a confirmed freeze is True."""
        o = self.of("eth_heartbeat")
        return o is not None and o.verdict is Verdict.UNHEALTHY

    @property
    def frozen_chips(self) -> int:
        """Chips present on the bus but ARC-frozen — the two-sample ``stalled`` list a heartbeat
        verdict records. Mirrors ``server._frozen_chip_count``, read from the same evidence."""
        o = self.of("heartbeat")
        if o is None:
            return 0
        return len(o.evidence.get("stalled") or [])

    def as_evidence(self) -> dict:
        """The legacy ``{"heartbeat": ..., "snapshot": ..., "eth_heartbeat": ..., "fabric": ...}``
        dict the durable journal (``health_event("gate", evidence=...)``, ``capture_incident``)
        and the test suite's fakes are built against. Reshapes each Observation back into the
        pre-extraction per-probe dict rather than exposing the Observation vocabulary to callers
        that predate it.
        """
        out: dict = {}
        for o in self.observations:
            label = _LEGACY_LABELS.get(o.monitor, o.monitor)
            if o.monitor == "heartbeat":
                # The one probe whose legacy dict names the verdict "verdict", not "ok" — kept
                # verbatim so a heartbeat evidence dict is byte-for-byte what it always was.
                out[label] = {"verdict": o.verdict.value, "detail": o.detail, **o.evidence}
            else:
                ok = True if o.verdict is Verdict.HEALTHY else False if o.verdict is Verdict.UNHEALTHY else None
                out[label] = {"ok": ok, "detail": o.detail, **o.evidence}
        return out

    @classmethod
    def from_evidence(cls, evidence: Mapping[str, Any], *, phase: str, expected: int = 0) -> "HealthState":
        """The read side of :meth:`as_evidence`: reconstruct the ``Observation``s a legacy
        ``{"heartbeat": ..., "snapshot": ..., "eth_heartbeat": ..., "fabric": ...}`` dict was
        built from, so a caller holding only that dict — every ``patch_recovery("_verify_device",
        ...)`` test fake, which returns this shape directly and never calls
        :meth:`HealthMonitor.update`, and the durable journal's own records — can still use the
        typed accessors instead of a raw dict lookup. A key the dict omits produces no
        Observation, the same as a probe this pass never ran: most fakes hand back only the one
        probe their scenario cares about, and ``fabric_ok``/``eth_frozen``/``frozen_chips`` must
        read that absence exactly the way they read a real skip.
        """
        observations = []
        for label, d in evidence.items():
            if not isinstance(d, Mapping):
                continue
            monitor = _LEGACY_LABELS_REVERSE.get(label, label)
            detail = d.get("detail", "")
            if monitor == "heartbeat":
                # The one probe whose legacy dict names the verdict "verdict", not "ok" — see
                # as_evidence()'s own comment on this asymmetry.
                try:
                    verdict = Verdict(d.get("verdict"))
                except ValueError:
                    continue  # not a real verdict; nothing to reconstruct from this entry
                rest = {k: v for k, v in d.items() if k not in ("verdict", "detail")}
            else:
                ok = d.get("ok")
                verdict = Verdict.HEALTHY if ok is True else Verdict.UNHEALTHY if ok is False else Verdict.SKIPPED
                rest = {k: v for k, v in d.items() if k not in ("ok", "detail")}
            observations.append(
                Observation(monitor=monitor, verdict=verdict, detail=detail, evidence=rest, phase=phase)
            )
        return cls(phase=phase, at=datetime.now(), expected=expected, observations=tuple(observations))
