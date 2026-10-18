"""Choose one stored correction. The model does not write the words."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from caddiej.swing_reference.model import SwingReference

_GOOD = frozenset({"high", "medium"})


@dataclass(frozen=True, slots=True)
class SelectedCorrection:
    checkpoint_id: str
    rule_id: str
    feedback: str
    correction: str


@dataclass(frozen=True, slots=True)
class SelectedPositive:
    checkpoint_id: str
    text: str


def select_correction(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    checkpoint_id: str | None,
) -> tuple[SelectedCorrection | None, str]:
    """Return the one applicable rule, or none, plus why.

    Zero matches and several matches are both no correction. A smaller
    checkpoint is not substituted.
    """

    if not checkpoint_id:
        return None, "no checkpoint"
    checkpoint = reference.checkpoints.get(checkpoint_id)
    finding = findings.get(checkpoint_id)
    if checkpoint is None or finding is None:
        return None, "checkpoint is not in the assessment"
    if checkpoint.mode != "visual":
        return None, "checkpoint is not visually graded"
    if finding.get("status") != "deviation" or finding.get("certainty") not in _GOOD:
        return None, "deviation is not supported at high or medium certainty"
    if not finding.get("evidence_frames"):
        return None, "deviation has no evidence"
    matched = [
        rule
        for rule in checkpoint.rules
        if finding.get("problem") in rule["when"]["problems"]
        and _requirements_met(rule, findings)
    ]
    if not matched:
        return None, "no stored rule matches the problem and its requirements"
    if len(matched) > 1:
        return None, "more than one stored rule matches; none is chosen"
    rule = matched[0]
    return (
        SelectedCorrection(
            checkpoint_id=checkpoint_id,
            rule_id=str(rule["id"]),
            feedback=str(rule["feedback"]),
            correction=str(rule["correction"]),
        ),
        "stored rule matches",
    )


def select_positive(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    checkpoint_id: str | None,
) -> SelectedPositive | None:
    """Approved praise for one checkpoint that is visibly near the reference."""

    if not checkpoint_id:
        return None
    checkpoint = reference.checkpoints.get(checkpoint_id)
    finding = findings.get(checkpoint_id or "")
    if checkpoint is None or finding is None or not checkpoint.positive_feedback:
        return None
    if checkpoint.mode != "visual":
        return None
    if (
        finding.get("status") != "near_reference"
        or finding.get("certainty") not in _GOOD
    ):
        return None
    if not finding.get("evidence_frames"):
        return None
    return SelectedPositive(checkpoint_id, checkpoint.positive_feedback)


def praise_candidate(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    proposed: dict[str, Any] | None,
) -> SelectedPositive | None:
    """One praise line, and only when the choice is determined.

    A verified proposal is used. Otherwise a single eligible checkpoint is
    used. Several eligible checkpoints are left unpicked.
    """

    verified = verified_positive(reference, findings, proposed)
    if verified is not None:
        return verified
    eligible = [
        checkpoint_id
        for checkpoint_id in reference.checkpoints
        if select_positive(reference, findings, checkpoint_id) is not None
    ]
    if len(eligible) != 1:
        return None
    return select_positive(reference, findings, eligible[0])


def verified_positive(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    proposed: dict[str, Any] | None,
) -> SelectedPositive | None:
    """Accept a proposed positive only when the stored text and finding agree."""

    if not proposed:
        return None
    selected = select_positive(
        reference, findings, str(proposed.get("checkpoint_id") or "")
    )
    if selected is None or selected.text != proposed.get("text"):
        return None
    return selected


def _requirements_met(
    rule: dict[str, Any], findings: dict[str, dict[str, Any]]
) -> bool:
    for requirement in rule["when"].get("requires") or []:
        finding = findings.get(requirement["checkpoint_id"])
        if finding is None:
            return False
        if finding.get("status") != requirement["status"]:
            return False
        if finding.get("certainty") not in _GOOD:
            return False
        if (
            "problem" in requirement
            and finding.get("problem") != requirement["problem"]
        ):
            return False
    return True
