"""Turn one assessment and the current session into one coaching decision.

The engine reads findings and a rulebook. It does not read images, and it
does not add a finding the assessment did not already contain.
"""

from __future__ import annotations

from contracts import STATUS_FAILED, VisualAssessmentResult

from session_coach.assessment import validate_assessment
from session_coach.models import (
    ADVANCE_FOCUS,
    COMPARATIVE_IMPROVEMENT,
    CONTINUE_FOCUS,
    CONTRADICTED,
    FOCUS_MASTERED,
    IMPROVEMENT_CONFIRMED,
    IMPROVEMENT_OBSERVED,
    NO_COACHING,
    POSITIVE_OBSERVATION,
    PROGRESS_UNKNOWN,
    REASSESS_FOCUS,
    REINFORCE_PROGRESS,
    SCHEMA_VERSION,
    START_FOCUS,
    CoachingRule,
    CoachingRulebook,
    CoachingSessionState,
    DecisionRecord,
    ProgressMark,
    SessionCoachingDecision,
)
from session_coach.priority import FocusPrioritizer
from session_coach.progress import _COUNTED, ProgressEvaluator, _signal, follow_claim

_SCALE = "uncalibrated_heuristic"


class CoachingEngine:
    def __init__(
        self,
        rulebook: CoachingRulebook,
        *,
        prioritizer: FocusPrioritizer | None = None,
        progress: ProgressEvaluator | None = None,
    ) -> None:
        self.rulebook = rulebook
        self.prioritizer = prioritizer or FocusPrioritizer()
        self.progress = progress or ProgressEvaluator()

    def decide(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
    ) -> SessionCoachingDecision:
        if (
            state.rulebook_version
            and state.rulebook_version != self.rulebook.rulebook_version
        ):
            return self._held(
                assessment,
                state,
                "rulebook version does not match the session",
                ("stale_rulebook",),
            )
        problems = list(validate_assessment(assessment))
        if assessment.status == STATUS_FAILED:
            problems.append("assessment failed")
        if problems:
            return self._held(
                assessment, state, "assessment was not accepted", tuple(problems)
            )
        if assessment.sequence is None:
            return self._held(
                assessment, state, "assessment has no sequence", ("missing_sequence",)
            )
        if assessment.shot_id in state.assessment_shot_ids:
            return self._held(assessment, state, "duplicate shot", ("duplicate_shot",))
        if (
            state.last_sequence is not None
            and assessment.sequence <= state.last_sequence
        ):
            return self._held(
                assessment, state, "assessment is out of order", ("out_of_order",)
            )
        return self._accepted(assessment, state)

    def _accepted(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
    ) -> SessionCoachingDecision:
        focus = state.active_focus
        current = _weakness(assessment, focus) if focus else None
        follow = assessment.focus_follow_up
        claim = (
            follow_claim(follow)
            if focus and follow is not None and follow.previous_focus_id == focus
            else None
        )
        if claim == "regressed":
            return self._reassess(assessment, state, focus)
        if (
            claim is None
            and focus
            and current is not None
            and current.comparison == CONTRADICTED
        ):
            return self._reassess(assessment, state, focus)
        progress = self.progress.evaluate(
            state.progress_marks,
            assessment,
            focus,
            self.rulebook.rule_for(focus) if focus else None,
        )
        if focus and progress == FOCUS_MASTERED:
            return self._advance(assessment, state, focus, progress, _kind(claim))
        if focus and progress in {IMPROVEMENT_OBSERVED, IMPROVEMENT_CONFIRMED}:
            return self._reinforce(assessment, state, focus, progress, _kind(claim))
        if focus:
            return self._continue(assessment, state, focus)
        chosen, provisional, notes = self.prioritizer.choose(assessment, self.rulebook)
        if chosen is None:
            return self._record(
                assessment,
                state,
                decision_type=NO_COACHING,
                active=None,
                previous=None,
                rule=None,
                reason="no eligible weakness",
                progress=PROGRESS_UNKNOWN,
                provisional=False,
                notes=notes,
                delivered=False,
            )
        return self._record(
            assessment,
            state,
            decision_type=START_FOCUS,
            active=chosen.finding_id,
            previous=None,
            rule=chosen,
            reason="started the highest ranked eligible weakness",
            progress=PROGRESS_UNKNOWN,
            provisional=provisional,
            notes=notes,
            delivered=True,
            selection_reason="started the highest ranked eligible weakness",
        )

    def _continue(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        focus: str,
    ) -> SessionCoachingDecision:
        rule = self.rulebook.rule_for(focus)
        return self._record(
            assessment,
            state,
            decision_type=CONTINUE_FOCUS,
            active=focus,
            previous=focus,
            rule=rule,
            reason="progress is unknown, so the focus stays",
            progress=PROGRESS_UNKNOWN,
            provisional=state.priority_provisional,
            notes=(),
            delivered=True,
            feedback_kind=(
                POSITIVE_OBSERVATION if _supported_strength(assessment) else ""
            ),
        )

    def _reinforce(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        focus: str,
        progress: str,
        feedback_kind: str,
    ) -> SessionCoachingDecision:
        reason = "assessment comparison says improved"
        if feedback_kind == POSITIVE_OBSERVATION:
            reason = "the current swing shows the requested movement"
        elif feedback_kind == COMPARATIVE_IMPROVEMENT:
            reason = "previous frames support a change on the active focus"
        return self._record(
            assessment,
            state,
            decision_type=REINFORCE_PROGRESS,
            active=focus,
            previous=focus,
            rule=self.rulebook.rule_for(focus),
            reason=reason,
            progress=progress,
            provisional=state.priority_provisional,
            notes=(),
            delivered=True,
            feedback_kind=feedback_kind,
        )

    def _advance(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        focus: str,
        progress: str,
        feedback_kind: str,
    ) -> SessionCoachingDecision:
        chosen, provisional, notes = self.prioritizer.choose(
            assessment, self.rulebook, skip=frozenset({focus})
        )
        if chosen is None:
            return self._record(
                assessment,
                state,
                decision_type=NO_COACHING,
                active=None,
                previous=focus,
                rule=None,
                reason=(
                    "focus met the rulebook mastered count and no other weakness is eligible"
                ),
                progress=progress,
                provisional=False,
                notes=notes,
                delivered=False,
                clear_focus=True,
                feedback_kind=feedback_kind,
            )
        return self._record(
            assessment,
            state,
            decision_type=ADVANCE_FOCUS,
            active=chosen.finding_id,
            previous=focus,
            rule=chosen,
            reason="focus met the rulebook mastered count",
            progress=progress,
            provisional=provisional,
            notes=notes,
            delivered=True,
            selection_reason="focus met the rulebook mastered count",
            feedback_kind=feedback_kind,
        )

    def _reassess(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        focus: str,
    ) -> SessionCoachingDecision:
        chosen, provisional, notes = self.prioritizer.choose(
            assessment, self.rulebook, skip=frozenset({focus})
        )
        reason = "assessment comparison contradicted the active focus"
        if chosen is None:
            return self._record(
                assessment,
                state,
                decision_type=REASSESS_FOCUS,
                active=None,
                previous=focus,
                rule=None,
                reason=reason,
                progress=PROGRESS_UNKNOWN,
                provisional=False,
                notes=notes,
                delivered=False,
                clear_focus=True,
            )
        return self._record(
            assessment,
            state,
            decision_type=REASSESS_FOCUS,
            active=chosen.finding_id,
            previous=focus,
            rule=chosen,
            reason=reason,
            progress=PROGRESS_UNKNOWN,
            provisional=provisional,
            notes=notes,
            delivered=True,
            selection_reason=reason,
        )

    def _record(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        *,
        decision_type: str,
        active: str | None,
        previous: str | None,
        rule: CoachingRule | None,
        reason: str,
        progress: str,
        provisional: bool,
        notes: tuple[str, ...],
        delivered: bool,
        selection_reason: str | None = None,
        clear_focus: bool = False,
        feedback_kind: str = "",
    ) -> SessionCoachingDecision:
        finding = _weakness(assessment, active)
        mark = _progress_mark(assessment, decision_type, previous)
        start = state.focus_start_shot
        kept_reason = state.focus_selection_reason
        if decision_type in {START_FOCUS, ADVANCE_FOCUS, REASSESS_FOCUS}:
            start = assessment.shot_id if active else None
            kept_reason = selection_reason or ""
        if clear_focus:
            start = None
            kept_reason = ""
        candidate, _, _ = self.prioritizer.choose(
            assessment,
            self.rulebook,
            skip=frozenset({active} if active else ()),
        )
        next_state = CoachingSessionState(
            schema_version=SCHEMA_VERSION,
            session_id=state.session_id,
            rulebook_version=self.rulebook.rulebook_version,
            active_focus=active,
            focus_start_shot=start,
            focus_selection_reason=kept_reason,
            assessment_shot_ids=state.assessment_shot_ids + (assessment.shot_id,),
            decisions=state.decisions
            + (
                DecisionRecord(
                    shot_id=assessment.shot_id,
                    decision_type=decision_type,
                    active_focus=active,
                    rule_id=rule.finding_id if rule else None,
                    reason=reason,
                    progress=progress,
                    provisional=provisional,
                    feedback_kind=feedback_kind,
                ),
            ),
            progress_marks=state.progress_marks + ((mark,) if mark else ()),
            progress=(
                PROGRESS_UNKNOWN
                if decision_type in {START_FOCUS, ADVANCE_FOCUS, REASSESS_FOCUS}
                else progress
            ),
            candidate_next_focus=candidate.finding_id if candidate else None,
            delivered_shot_ids=state.delivered_shot_ids
            + ((assessment.shot_id,) if delivered else ()),
            last_sequence=assessment.sequence,
            priority_provisional=provisional,
        )
        return self._decision(
            assessment,
            next_state,
            status="ok",
            decision_type=decision_type,
            active=active,
            previous=previous,
            rule=rule,
            reason=reason,
            progress=progress,
            provisional=provisional,
            notes=notes,
            confidence=None if finding is None else finding.confidence,
            errors=(),
            feedback_kind=feedback_kind,
        )

    def _held(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        reason: str,
        errors: tuple[str, ...],
    ) -> SessionCoachingDecision:
        rule = (
            self.rulebook.rule_for(state.active_focus) if state.active_focus else None
        )
        return self._decision(
            assessment,
            state,
            status="failed",
            decision_type=NO_COACHING,
            active=state.active_focus,
            previous=state.active_focus,
            rule=rule,
            reason=reason,
            progress=state.progress,
            provisional=state.priority_provisional,
            notes=(),
            confidence=None,
            errors=errors,
            feedback_kind="",
        )

    def _decision(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        *,
        status: str,
        decision_type: str,
        active: str | None,
        previous: str | None,
        rule: CoachingRule | None,
        reason: str,
        progress: str,
        provisional: bool,
        notes: tuple[str, ...],
        confidence: float | None,
        errors: tuple[str, ...],
        feedback_kind: str = "",
    ) -> SessionCoachingDecision:
        uncertainty = "confidence is an uncalibrated heuristic"
        if notes:
            uncertainty = f"{uncertainty}; " + "; ".join(notes)
        elif confidence is None:
            uncertainty = "progress is unknown"
        return SessionCoachingDecision(
            schema_version=SCHEMA_VERSION,
            shot_id=assessment.shot_id,
            status=status,
            decision_type=decision_type,
            active_focus=active,
            previous_focus=previous,
            rule_id=rule.finding_id if rule and decision_type != NO_COACHING else None,
            cue_id=rule.cue_id if rule and decision_type != NO_COACHING else None,
            assessment_shot_id=assessment.shot_id,
            reason=reason,
            progress=progress,
            confidence=confidence,
            confidence_scale=_SCALE if confidence is not None else "",
            provisional_priority=provisional,
            uncertainty=uncertainty,
            errors=errors,
            resulting_state=state,
            feedback_kind=feedback_kind,
        )


def _weakness(assessment: VisualAssessmentResult, focus: str | None):
    if not focus:
        return None
    for finding in assessment.findings:
        if finding.kind == "weakness" and finding.fault_id == focus:
            return finding
    return None


def _kind(claim: str | None) -> str:
    if claim == "comparative":
        return COMPARATIVE_IMPROVEMENT
    if claim == "execution":
        return POSITIVE_OBSERVATION
    return ""


def _supported_strength(assessment: VisualAssessmentResult) -> bool:
    return any(
        finding.kind == "strength"
        and finding.evidence_frames
        and not finding.unverified
        for finding in assessment.findings
    )


def _progress_mark(
    assessment: VisualAssessmentResult, decision_type: str, previous: str | None
) -> ProgressMark | None:
    if decision_type == START_FOCUS or not previous:
        return None
    if decision_type in {REINFORCE_PROGRESS, ADVANCE_FOCUS, NO_COACHING}:
        signal = _signal(assessment, previous)
        if signal in _COUNTED:
            return ProgressMark(
                shot_id=assessment.shot_id,
                finding_id=previous,
                comparison=signal,
            )
    return _mark(assessment, previous)


def _mark(assessment: VisualAssessmentResult, focus: str | None) -> ProgressMark | None:
    follow = assessment.focus_follow_up
    if follow is not None and follow.previous_focus_id == focus:
        return None
    finding = _weakness(assessment, focus)
    if finding is None or finding.comparison is None:
        return None
    if finding.comparison == "not_comparable":
        return None
    if finding.fault_id is None:
        return None
    return ProgressMark(
        shot_id=assessment.shot_id,
        finding_id=finding.fault_id,
        comparison=finding.comparison,
    )
