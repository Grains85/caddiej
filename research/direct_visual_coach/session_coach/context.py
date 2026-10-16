"""Build the small previous-shot note the next assessment may see.

The note is derived from the saved assessment, the decision, and delivery.
It does not copy session history, and it does not invent a cue.
"""

from __future__ import annotations

from contracts import VisualAssessmentResult

from session_coach.models import (
    DELIVERED,
    DELIVERY_UNKNOWN,
    NO_COACHING,
    NOT_DELIVERED,
    SCHEMA_VERSION,
    CoachingRulebook,
    CoachingSessionState,
    DeliveryBrief,
    PreviousCoachingContext,
)

MAX_OBSERVATION_CHARS = 160
MAX_CUE_CHARS = 200
CONTEXT_FIELDS = (
    "schema_version",
    "active_focus",
    "previous_cue",
    "previous_observation",
    "delivery_status",
)


def build_previous_context(
    state: CoachingSessionState,
    rulebook: CoachingRulebook,
    assessment: VisualAssessmentResult | None,
    delivery: DeliveryBrief | None = None,
) -> PreviousCoachingContext | None:
    """Return the context for the next shot, or nothing on the first shot."""

    if not state.active_focus or not state.decisions:
        return None
    last = state.decisions[-1]
    if last.decision_type == NO_COACHING or last.active_focus != state.active_focus:
        return None
    cue, status = _delivered_cue(state.active_focus, rulebook, delivery)
    return PreviousCoachingContext(
        schema_version=SCHEMA_VERSION,
        active_focus=state.active_focus,
        previous_cue=cue,
        previous_observation=_observation(assessment, state.active_focus),
        delivery_status=status,
    )


def _delivered_cue(
    focus: str,
    rulebook: CoachingRulebook,
    delivery: DeliveryBrief | None,
) -> tuple[str | None, str]:
    rule = rulebook.rule_for(focus)
    if delivery is None or rule is None:
        return None, DELIVERY_UNKNOWN
    if not delivery.presented:
        return None, NOT_DELIVERED
    if delivery.cue_id != rule.cue_id:
        return None, DELIVERY_UNKNOWN
    text = rule.speech_text.strip()
    if not text:
        return None, DELIVERY_UNKNOWN
    return text[:MAX_CUE_CHARS], DELIVERED


def _observation(assessment: VisualAssessmentResult | None, focus: str) -> str | None:
    if assessment is None:
        return None
    for finding in assessment.findings:
        if finding.kind != "weakness" or finding.fault_id != focus:
            continue
        if finding.unverified or not finding.evidence_frames:
            return None
        note = finding.note.strip()
        if not note:
            return None
        return note[:MAX_OBSERVATION_CHARS]
    return None
