"""Copy approved cue text. This module does not speak and does not draw."""

from __future__ import annotations

from session_coach.models import (
    NO_COACHING,
    CoachingRulebook,
    DeliveryBrief,
    SessionCoachingDecision,
)


def brief_for(
    decision: SessionCoachingDecision, rulebook: CoachingRulebook
) -> DeliveryBrief:
    if (
        decision.status != "ok"
        or decision.decision_type == NO_COACHING
        or not decision.cue_id
    ):
        return DeliveryBrief(
            shot_id=decision.shot_id,
            decision_type=decision.decision_type,
            presented=False,
            hud_text="",
            speech_text="",
            cue_id=None,
            suppression="no_coaching",
        )
    rule = rulebook.rule_for(decision.active_focus or "")
    if rule is None or rule.cue_id != decision.cue_id:
        return DeliveryBrief(
            shot_id=decision.shot_id,
            decision_type=decision.decision_type,
            presented=False,
            hud_text="",
            speech_text="",
            cue_id=None,
            suppression="missing_rule",
        )
    return DeliveryBrief(
        shot_id=decision.shot_id,
        decision_type=decision.decision_type,
        presented=True,
        hud_text=rule.hud_text,
        speech_text=rule.speech_text,
        cue_id=rule.cue_id,
        suppression=None,
    )
