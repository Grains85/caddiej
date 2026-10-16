"""Compare an active focus with a later assessment.

A missing finding is not improvement. A lower confidence is not improvement.
Only an explicit comparison on the assessment moves the progress label, and
only by the counts stored in the rule.
"""

from __future__ import annotations

from contracts import (
    CHANGE_IMPROVED,
    CHANGE_REGRESSED,
    DESIRED_MOVEMENT_VISIBLE,
    STAGE_CORRESPONDENCE_RELIABLE,
    AssessmentFinding,
    FocusFollowUp,
    VisualAssessmentResult,
)

from session_coach.models import (
    FOCUS_MASTERED,
    IMPROVED,
    IMPROVEMENT_CONFIRMED,
    IMPROVEMENT_OBSERVED,
    PROGRESS_UNKNOWN,
    CoachingRule,
    ProgressMark,
)

_COUNTED = frozenset({IMPROVED, DESIRED_MOVEMENT_VISIBLE})


class ProgressEvaluator:
    def evaluate(
        self,
        marks: tuple[ProgressMark, ...],
        assessment: VisualAssessmentResult,
        focus: str | None,
        rule: CoachingRule | None,
    ) -> str:
        if focus is None or rule is None:
            return PROGRESS_UNKNOWN
        signal = _signal(assessment, focus)
        if signal not in _COUNTED:
            return PROGRESS_UNKNOWN
        count = 1 + sum(
            1
            for mark in marks
            if mark.finding_id == focus and mark.comparison in _COUNTED
        )
        if count >= rule.mastered_after:
            return FOCUS_MASTERED
        if count >= rule.confirmed_after:
            return IMPROVEMENT_CONFIRMED
        if count >= rule.observed_after:
            return IMPROVEMENT_OBSERVED
        return PROGRESS_UNKNOWN


def follow_claim(follow: FocusFollowUp) -> str:
    """How far the follow-up may move the session.

    `comparative` needs previous frames and reliable stage correspondence.
    `execution` means the requested movement is visible in this swing only.
    """

    compared = (
        follow.previous_image_count > 0
        and follow.stage_correspondence == STAGE_CORRESPONDENCE_RELIABLE
    )
    if follow.change_since_previous == CHANGE_REGRESSED and compared:
        return "regressed"
    if follow.change_since_previous == CHANGE_IMPROVED and compared:
        return "comparative"
    if follow.current_focus_status == DESIRED_MOVEMENT_VISIBLE:
        return "execution"
    return "none"


def _signal(assessment: VisualAssessmentResult, focus: str) -> str | None:
    follow = assessment.focus_follow_up
    if follow is not None and follow.previous_focus_id == focus:
        claim = follow_claim(follow)
        if claim == "comparative":
            return IMPROVED
        if claim == "execution":
            return DESIRED_MOVEMENT_VISIBLE
        return None
    finding = _match(assessment, focus)
    if finding is not None and finding.comparison == IMPROVED:
        return IMPROVED
    return None


def _match(assessment: VisualAssessmentResult, focus: str) -> AssessmentFinding | None:
    for finding in assessment.findings:
        if finding.fault_id == focus and finding.kind == "weakness":
            return finding
    return None
