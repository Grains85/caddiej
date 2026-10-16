"""Session-coaching contracts.

These types sit downstream of a visual assessment. They do not carry image
bytes, and they do not carry a measurement the assessment did not already
state.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "1"

START_FOCUS = "START_FOCUS"
CONTINUE_FOCUS = "CONTINUE_FOCUS"
REINFORCE_PROGRESS = "REINFORCE_PROGRESS"
ADVANCE_FOCUS = "ADVANCE_FOCUS"
REASSESS_FOCUS = "REASSESS_FOCUS"
NO_COACHING = "NO_COACHING"
DECISION_TYPES = frozenset(
    {
        START_FOCUS,
        CONTINUE_FOCUS,
        REINFORCE_PROGRESS,
        ADVANCE_FOCUS,
        REASSESS_FOCUS,
        NO_COACHING,
    }
)

PROGRESS_UNKNOWN = "PROGRESS_UNKNOWN"
IMPROVEMENT_OBSERVED = "IMPROVEMENT_OBSERVED"
IMPROVEMENT_CONFIRMED = "IMPROVEMENT_CONFIRMED"
FOCUS_MASTERED = "FOCUS_MASTERED"
PROGRESS_VALUES = frozenset(
    {
        PROGRESS_UNKNOWN,
        IMPROVEMENT_OBSERVED,
        IMPROVEMENT_CONFIRMED,
        FOCUS_MASTERED,
    }
)

STILL_SUPPORTED = "still_supported"
IMPROVED = "improved"
CONTRADICTED = "contradicted"
NOT_COMPARABLE = "not_comparable"
COMPARISONS = frozenset({STILL_SUPPORTED, IMPROVED, CONTRADICTED, NOT_COMPARABLE})


@dataclass(frozen=True, slots=True)
class CauseNote:
    """A possible cause. V1 accepts only unverified notes."""

    statement: str
    status: str


@dataclass(frozen=True, slots=True)
class CoachingRule:
    finding_id: str
    kind: str
    priority_rank: int
    min_confidence: float
    actionable: bool
    cue_id: str
    hud_text: str
    speech_text: str
    possible_causes: tuple[CauseNote, ...]
    contraindicated_while: tuple[str, ...]
    observed_after: int
    confirmed_after: int
    mastered_after: int
    source: str


@dataclass(frozen=True, slots=True)
class CoachingRulebook:
    schema_version: str
    rulebook_version: str
    approval_status: str
    rules: tuple[CoachingRule, ...]

    def rule_for(self, finding_id: str) -> CoachingRule | None:
        for rule in self.rules:
            if rule.finding_id == finding_id:
                return rule
        return None


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    """One extracted coaching statement. It is not an active rule."""

    record_id: str
    source_video: str
    coach_id: str
    start_seconds: float
    end_seconds: float
    original_statement: str
    extracted_observation: str
    possible_cause: str
    suggested_correction: str
    suggested_drill: str
    swing_stages: tuple[str, ...]
    uncertainty: str
    review_status: str
    contradictions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressMark:
    shot_id: str
    finding_id: str
    comparison: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    shot_id: str
    decision_type: str
    active_focus: str | None
    rule_id: str | None
    reason: str
    progress: str
    provisional: bool
    feedback_kind: str = ""


@dataclass(frozen=True, slots=True)
class CoachingSessionState:
    schema_version: str
    session_id: str
    rulebook_version: str
    active_focus: str | None
    focus_start_shot: str | None
    focus_selection_reason: str
    assessment_shot_ids: tuple[str, ...]
    decisions: tuple[DecisionRecord, ...]
    progress_marks: tuple[ProgressMark, ...]
    progress: str
    candidate_next_focus: str | None
    delivered_shot_ids: tuple[str, ...]
    last_sequence: int | None
    priority_provisional: bool


@dataclass(frozen=True, slots=True)
class SessionCoachingDecision:
    """One coaching step. `resulting_state` is the state after this step."""

    schema_version: str
    shot_id: str
    status: str
    decision_type: str
    active_focus: str | None
    previous_focus: str | None
    rule_id: str | None
    cue_id: str | None
    assessment_shot_id: str
    reason: str
    progress: str
    confidence: float | None
    confidence_scale: str
    provisional_priority: bool
    uncertainty: str
    errors: tuple[str, ...]
    resulting_state: CoachingSessionState
    feedback_kind: str = ""


DELIVERED = "delivered"
NOT_DELIVERED = "not_delivered"
DELIVERY_UNKNOWN = "unknown"
DELIVERY_STATUSES = frozenset({DELIVERED, NOT_DELIVERED, DELIVERY_UNKNOWN})

POSITIVE_OBSERVATION = "positive_observation"
COMPARATIVE_IMPROVEMENT = "comparative_improvement"


@dataclass(frozen=True, slots=True)
class PreviousCoachingContext:
    """The only previous-shot text a later assessment is given.

    `previous_cue` is set only when that cue was actually presented. A selected
    cue that was not delivered, or whose delivery is unknown, is left empty.
    """

    schema_version: str
    active_focus: str
    previous_cue: str | None
    previous_observation: str | None
    delivery_status: str


@dataclass(frozen=True, slots=True)
class DeliveryBrief:
    """Words copied from an approved rule. Delivery does not write new advice."""

    shot_id: str
    decision_type: str
    presented: bool
    hud_text: str
    speech_text: str
    cue_id: str | None
    suppression: str | None


def empty_state(session_id: str) -> CoachingSessionState:
    return CoachingSessionState(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        rulebook_version="",
        active_focus=None,
        focus_start_shot=None,
        focus_selection_reason="",
        assessment_shot_ids=(),
        decisions=(),
        progress_marks=(),
        progress=PROGRESS_UNKNOWN,
        candidate_next_focus=None,
        delivered_shot_ids=(),
        last_sequence=None,
        priority_provisional=False,
    )
