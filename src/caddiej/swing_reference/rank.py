"""Severity ranking from the reference model.

A higher severity is a larger visible deviation. It is not a claim about ball
flight and it is not a root cause. The reference ranks by that severity scale
only. Two eligible deviations that share the highest severity are tied, because
nothing else in the reference orders them. Confidence is not a rank.

Validation and the coaching decision both use this result. A model ranking
that disagrees with it is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from caddiej.swing_reference.model import SwingReference

_ORDER = {"minor": 0, "clear": 1, "large": 2}
_GOOD = frozenset({"high", "medium"})
_JUDGED = frozenset({"near_reference", "deviation"})


@dataclass(frozen=True, slots=True)
class DeviationRanking:
    status: str
    primary_checkpoint_id: str | None
    secondary_checkpoint_ids: tuple[str, ...]
    note: str


def rank_findings(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
) -> DeviationRanking:
    eligible = [
        checkpoint_id
        for checkpoint_id, finding in findings.items()
        if _eligible(reference, checkpoint_id, finding)
    ]
    judged = [
        checkpoint_id
        for checkpoint_id, finding in findings.items()
        if finding.get("status") in _JUDGED and finding.get("certainty") in _GOOD
    ]
    if not judged:
        return DeviationRanking(
            "insufficient_evidence",
            None,
            (),
            "No visual finding has high or medium certainty.",
        )
    if not eligible:
        return DeviationRanking(
            "no_supported_deviation",
            None,
            (),
            "Supported visual judgments contain no eligible deviation.",
        )
    ranked = sorted(eligible, key=lambda item: _ORDER[str(findings[item]["severity"])])
    top = _ORDER[str(findings[ranked[-1]]["severity"])]
    leaders = tuple(
        checkpoint_id
        for checkpoint_id in eligible
        if _ORDER[str(findings[checkpoint_id]["severity"])] == top
    )
    if len(leaders) == 1:
        primary = leaders[0]
        secondary = tuple(
            checkpoint_id for checkpoint_id in eligible if checkpoint_id != primary
        )
        return DeviationRanking(
            "unique_primary",
            primary,
            secondary,
            "One eligible deviation has a higher severity than the others.",
        )
    return DeviationRanking(
        "tied",
        None,
        leaders,
        "More than one eligible deviation shares the highest severity.",
    )


def ranking_matches(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    result: dict[str, Any],
) -> bool:
    """True when the assessment names the same ranking this module derives."""

    ranked = rank_findings(reference, findings)
    secondary = [str(item) for item in result.get("secondary_checkpoint_ids") or ()]
    if result.get("ranking_status") != ranked.status:
        return False
    if result.get("primary_checkpoint_id") != ranked.primary_checkpoint_id:
        return False
    if ranked.status != "tied":
        return True
    return set(secondary) == set(ranked.secondary_checkpoint_ids) and len(
        secondary
    ) == len(ranked.secondary_checkpoint_ids)


def exclusion_reason(
    reference: SwingReference,
    checkpoint_id: str,
    finding: dict[str, Any],
) -> str | None:
    """Why a finding is not an eligible deviation.

    None means it is eligible. `rank_findings` uses these same predicates and
    is unchanged by explaining them.
    """

    checkpoint = reference.checkpoints.get(checkpoint_id)
    if checkpoint is None:
        return "checkpoint is not in the reference model"
    if checkpoint.mode != "visual":
        return f"mode is {checkpoint.mode}, not visual"
    status = finding.get("status")
    if status != "deviation":
        return f"status is {status!r}, not deviation"
    certainty = finding.get("certainty")
    if certainty not in _GOOD:
        return f"certainty is {certainty!r}, not high or medium"
    return None


def _eligible(
    reference: SwingReference,
    checkpoint_id: str,
    finding: dict[str, Any],
) -> bool:
    return exclusion_reason(reference, checkpoint_id, finding) is None
