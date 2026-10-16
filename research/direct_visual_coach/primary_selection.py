"""Choose one catalog fault to coach. Research only.

The model may return several visually proven faults. This module picks one.
It does not read launch-monitor numbers, and it does not treat an earlier
phase as automatically better.

A catalog upstream link is ranking metadata. It never creates a fault, never
changes a confidence, and never supplies missing evidence. Only candidates
that were returned and already passed evidence validation may participate.

Policy:

1. A candidate below PROVEN_MIN confidence is not proven. It cannot be
   coached and it cannot explain another fault.
2. Mutually exclusive faults are resolved before anything is explained.
   If their confidences differ by more than STABLE_BAND, the lower one is
   dropped. If they are within STABLE_BAND, both are dropped.
3. A proven fault explains another proven fault only when it is listed in
   that fault's upstream_candidates and its confidence is at least
   EXPLAIN_SHARE of the downstream confidence. The link must be direct.
   An unreturned fault is not a bridge. Proven A does not explain proven C
   through an unreturned B.
4. Explained descendants are not coached. Among the faults that remain,
   the highest confidence wins. Faults within STABLE_BAND of that
   confidence are tied, so a small difference cannot switch the lesson.
5. Inside the tie, the fault that directly explains more proven candidates
   wins. A remaining tie uses alphabetical fault id. That order is stable
   and is not swing-phase order.
6. A weak upstream candidate therefore does not remove a strongly proven
   downstream fault, and it does not win on earliness alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fault_catalog import FAULTS_BY_ID, VisualFault

PROVEN_MIN = 0.60
EXPLAIN_SHARE = 0.85
STABLE_BAND = 0.05

POLICY = (
    "A candidate below 0.60 confidence is not proven. "
    "Mutually exclusive faults more than 0.05 apart keep the higher one; "
    "within 0.05, both are dropped. "
    "A proven fault explains another proven fault only when it is a direct "
    "catalog upstream candidate and its confidence is at least 0.85 of the "
    "downstream confidence. Unreturned faults are not traversed. "
    "An upstream link does not create a fault, raise confidence, or replace "
    "evidence. Explained descendants are not coached. "
    "The highest remaining confidence wins, and faults within 0.05 of it "
    "are tied. Inside the tie, the fault that directly explains more proven "
    "candidates wins, then alphabetical fault id. "
    "Earlier in the swing is not automatically better. "
    "Launch-monitor numbers are not used."
)


@dataclass(frozen=True, slots=True)
class Candidate:
    fault_id: str
    confidence: float


@dataclass(frozen=True, slots=True)
class RankedFault:
    fault_id: str
    confidence: float
    disposition: str
    explained_by: str | None = None
    explains: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "fault_id": self.fault_id,
            "confidence": self.confidence,
            "disposition": self.disposition,
            "explained_by": self.explained_by,
            "explains": list(self.explains),
        }


@dataclass(frozen=True, slots=True)
class PrimarySelection:
    fault_id: str | None
    reason: str
    ranked: tuple[RankedFault, ...]
    policy: str = POLICY

    def as_dict(self) -> dict[str, object]:
        return {
            "fault_id": self.fault_id,
            "reason": self.reason,
            "policy": self.policy,
            "ranked": [row.as_dict() for row in self.ranked],
        }


def select_primary(
    candidates: Sequence[Candidate | tuple[str, float]],
    *,
    faults: Mapping[str, VisualFault] = FAULTS_BY_ID,
) -> PrimarySelection:
    """The one fault to coach, or none. Same inputs always give the same result."""

    pooled = _pool(candidates)
    unknown = [fault_id for fault_id in pooled if fault_id not in faults]
    if unknown:
        listed = ", ".join(sorted(unknown))
        raise ValueError(f"unknown fault id: {listed}")

    below = {
        fault_id: confidence
        for fault_id, confidence in pooled.items()
        if confidence < PROVEN_MIN
    }
    proven = {
        fault_id: confidence
        for fault_id, confidence in pooled.items()
        if confidence >= PROVEN_MIN
    }
    contradicted = _contradictions(proven, faults)
    surviving = {
        fault_id: confidence
        for fault_id, confidence in proven.items()
        if fault_id not in contradicted
    }
    explained_by = _explained_by(surviving, faults)
    explains = _explains(explained_by)
    roots = {
        fault_id: confidence
        for fault_id, confidence in surviving.items()
        if fault_id not in explained_by
    }

    chosen, reason = _choose(roots, explains)
    ranked = _ranked(pooled, below, contradicted, explained_by, explains, roots, chosen)
    if chosen is None:
        reason = "no_proven_fault"
    return PrimarySelection(fault_id=chosen, reason=reason, ranked=tuple(ranked))


def _pool(candidates: Sequence[Candidate | tuple[str, float]]) -> dict[str, float]:
    pooled: dict[str, float] = {}
    for item in candidates:
        if isinstance(item, Candidate):
            fault_id, confidence = item.fault_id, item.confidence
        else:
            fault_id, confidence = item
        previous = pooled.get(fault_id)
        if previous is None or confidence > previous:
            pooled[fault_id] = float(confidence)
    return pooled


def _contradictions(
    proven: Mapping[str, float], faults: Mapping[str, VisualFault]
) -> set[str]:
    dropped: set[str] = set()
    for left_id, left_confidence in proven.items():
        for right_id in faults[left_id].mutually_exclusive_with:
            if right_id not in proven or left_id >= right_id:
                continue
            right_confidence = proven[right_id]
            if abs(left_confidence - right_confidence) <= STABLE_BAND:
                dropped.add(left_id)
                dropped.add(right_id)
            elif left_confidence < right_confidence:
                dropped.add(left_id)
            else:
                dropped.add(right_id)
    return dropped


def _explained_by(
    surviving: Mapping[str, float], faults: Mapping[str, VisualFault]
) -> dict[str, str]:
    """Direct returned upstream only. Confidence values are not changed."""

    explained: dict[str, str] = {}
    for fault_id, confidence in surviving.items():
        upstream = [
            other
            for other in faults[fault_id].upstream_candidates
            if other in surviving and surviving[other] >= EXPLAIN_SHARE * confidence
        ]
        if not upstream:
            continue
        explained[fault_id] = min(
            upstream, key=lambda other: (-surviving[other], other)
        )
    return explained


def _explains(explained_by: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for descendant, ancestor in explained_by.items():
        grouped.setdefault(ancestor, []).append(descendant)
    return {
        ancestor: tuple(sorted(descendants))
        for ancestor, descendants in grouped.items()
    }


def _choose(
    roots: Mapping[str, float], explains: Mapping[str, tuple[str, ...]]
) -> tuple[str | None, str]:
    if not roots:
        return None, "no_proven_fault"
    best = max(roots.values())
    band = [
        fault_id
        for fault_id, confidence in roots.items()
        if best - confidence <= STABLE_BAND
    ]
    band.sort(key=lambda fault_id: (-len(explains.get(fault_id, ())), fault_id))
    chosen = band[0]
    if explains.get(chosen):
        return chosen, "upstream_cause"
    if len(band) > 1:
        return chosen, "tie_break"
    return chosen, "highest_confidence"


def _ranked(
    pooled: Mapping[str, float],
    below: Mapping[str, float],
    contradicted: set[str],
    explained_by: Mapping[str, str],
    explains: Mapping[str, tuple[str, ...]],
    roots: Mapping[str, float],
    chosen: str | None,
) -> list[RankedFault]:
    rows: list[RankedFault] = []
    for fault_id in sorted(pooled):
        confidence = pooled[fault_id]
        if fault_id in below:
            disposition = "below_threshold"
            explained = None
            explained_ids: tuple[str, ...] = ()
        elif fault_id in contradicted:
            disposition = "contradicted"
            explained = None
            explained_ids = ()
        elif fault_id in explained_by:
            disposition = "explained"
            explained = explained_by[fault_id]
            explained_ids = ()
        elif fault_id == chosen:
            disposition = "primary"
            explained = None
            explained_ids = explains.get(fault_id, ())
        elif fault_id in roots:
            disposition = "eligible"
            explained = None
            explained_ids = explains.get(fault_id, ())
        else:
            disposition = "eligible"
            explained = None
            explained_ids = ()
        rows.append(
            RankedFault(
                fault_id=fault_id,
                confidence=confidence,
                disposition=disposition,
                explained_by=explained,
                explains=explained_ids,
            )
        )
    return rows
