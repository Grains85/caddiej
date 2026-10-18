"""Session policy for one ranked Ideal Swing assessment.

The engine keeps the focus, the progression counts, and the duplicate-shot
guards. Checkpoint rank and the spoken correction come from the reference
model. This module does not read images and does not author golf advice.
"""

from __future__ import annotations

from caddiej.swing_evidence.contracts import (
    CHANGE_UNKNOWN,
    DESIRED_MOVEMENT_VISIBLE,
    FAULT_STILL_VISIBLE,
    FOCUS_NOT_EVALUABLE,
    FOLLOW_UP_IMPROVED,
    FOLLOW_UP_NOT_EVALUABLE,
    FOLLOW_UP_NOT_IMPROVED,
    STAGE_CORRESPONDENCE_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_OK,
    AssessmentFinding,
    FocusFollowUp,
    FrameReference,
    Provenance,
    VisualAssessmentResult,
)
from caddiej.swing_session.models import (
    ADVANCE_FOCUS,
    COMPARATIVE_IMPROVEMENT,
    CONTINUE_FOCUS,
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
from caddiej.swing_session.progress import (
    _COUNTED,
    ProgressEvaluator,
    _signal,
)

_SCALE = "uncalibrated_heuristic"

# Session policy, not golf knowledge from ideal_swing.json. One execution is
# observed, the second is confirmed, the third may master the focus. They do
# not rank one checkpoint above another.
_SESSION_OBSERVED = 1
_SESSION_CONFIRMED = 2
_SESSION_MASTERED = 3


class _Missing:
    pass


_MISSING = _Missing()


class CoachingEngine:
    def __init__(
        self,
        rulebook: CoachingRulebook,
        *,
        progress: ProgressEvaluator | None = None,
    ) -> None:
        self.rulebook = rulebook
        self.progress = progress or ProgressEvaluator()

    def decide_reference(
        self,
        state: CoachingSessionState,
        *,
        shot_id: str,
        sequence: int | None,
        ranking_status: str,
        primary_checkpoint_id: str | None,
        tied_checkpoint_ids: tuple[str, ...] = (),
        focus_disposition: str = "not_evaluable",
        focus_observation: str = "",
        focus_frames: tuple[FrameReference, ...] = (),
        correction_for_active: str | None = None,
        correction_for_primary: str | None = None,
        positive_on_focus: bool = False,
        accepted: bool = True,
        errors: tuple[str, ...] = (),
    ) -> SessionCoachingDecision:
        """Session step for one validated reference-model assessment.

        `ranking_status` is the assessment's ranking. This method does not
        reorder checkpoints and does not break a tie. A new primary does not
        replace an active focus.
        """

        assessment = _reference_assessment(
            shot_id,
            sequence,
            state.active_focus,
            focus_disposition,
            focus_observation,
            focus_frames,
            failed=not accepted,
        )
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
        if not accepted or errors or assessment.status == STATUS_FAILED:
            return self._held(
                assessment,
                state,
                "assessment was not accepted",
                errors or ("assessment_rejected",),
            )
        if sequence is None:
            return self._held(
                assessment, state, "assessment has no sequence", ("missing_sequence",)
            )
        if shot_id in state.assessment_shot_ids:
            return self._held(assessment, state, "duplicate shot", ("duplicate_shot",))
        if state.last_sequence is not None and sequence <= state.last_sequence:
            return self._held(
                assessment, state, "assessment is out of order", ("out_of_order",)
            )
        return self._reference_step(
            assessment,
            state,
            ranking_status=ranking_status,
            primary=primary_checkpoint_id,
            tied=tied_checkpoint_ids,
            disposition=focus_disposition,
            correction_for_active=correction_for_active,
            correction_for_primary=correction_for_primary,
            positive_on_focus=positive_on_focus,
        )

    def _reference_step(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        *,
        ranking_status: str,
        primary: str | None,
        tied: tuple[str, ...],
        disposition: str,
        correction_for_active: str | None,
        correction_for_primary: str | None,
        positive_on_focus: bool,
    ) -> SessionCoachingDecision:
        focus = state.active_focus
        claim = "execution" if disposition == "execution" else None
        progress = PROGRESS_UNKNOWN
        if focus:
            progress = self.progress.evaluate(
                state.progress_marks,
                assessment,
                focus,
                _session_counts(focus),
            )
        notes = _tie_note(ranking_status, tied)
        if focus and progress == FOCUS_MASTERED:
            return self._reference_advance(
                assessment,
                state,
                focus,
                progress,
                _kind(claim),
                ranking_status,
                primary,
                notes,
                correction_for_primary,
            )
        if focus and progress in {IMPROVEMENT_OBSERVED, IMPROVEMENT_CONFIRMED}:
            return self._record(
                assessment,
                state,
                decision_type=REINFORCE_PROGRESS,
                active=focus,
                previous=focus,
                rule=_cue_rule(f"positive:{focus}") if positive_on_focus else None,
                reason="the current swing shows the requested movement",
                progress=progress,
                provisional=False,
                notes=notes,
                delivered=positive_on_focus,
                feedback_kind=POSITIVE_OBSERVATION if positive_on_focus else "",
                next_focus=_kept_candidate(ranking_status, primary, focus),
            )
        if focus:
            if disposition == "not_applicable":
                reason = (
                    "the active focus is outside the current player context "
                    "and is not counted as success"
                )
            elif disposition == "not_evaluable":
                reason = (
                    "the active focus cannot be evaluated on this swing "
                    "and is not counted as success"
                )
            elif ranking_status == "unique_primary" and primary and primary != focus:
                reason = (
                    "the active focus stays; a different primary does not replace it"
                )
            else:
                reason = "the deviation remains, so the focus stays"
            return self._record(
                assessment,
                state,
                decision_type=CONTINUE_FOCUS,
                active=focus,
                previous=focus,
                rule=(
                    _cue_rule(correction_for_active) if correction_for_active else None
                ),
                reason=reason,
                progress=PROGRESS_UNKNOWN,
                provisional=False,
                notes=notes,
                delivered=correction_for_active is not None,
                feedback_kind="",
                next_focus=_kept_candidate(ranking_status, primary, focus),
            )
        if ranking_status == "unique_primary" and primary:
            return self._record(
                assessment,
                state,
                decision_type=START_FOCUS,
                active=primary,
                previous=None,
                rule=(
                    _cue_rule(correction_for_primary)
                    if correction_for_primary
                    else None
                ),
                reason="started the unique primary deviation",
                progress=PROGRESS_UNKNOWN,
                provisional=False,
                notes=notes,
                delivered=correction_for_primary is not None,
                selection_reason="started the unique primary deviation",
                next_focus=None,
            )
        if ranking_status == "tied":
            return self._record(
                assessment,
                state,
                decision_type=NO_COACHING,
                active=None,
                previous=None,
                rule=None,
                reason="eligible deviations are tied",
                progress=PROGRESS_UNKNOWN,
                provisional=False,
                notes=notes,
                delivered=False,
                next_focus=None,
            )
        reason = (
            "no supported deviation"
            if ranking_status == "no_supported_deviation"
            else "insufficient evidence"
        )
        return self._record(
            assessment,
            state,
            decision_type=NO_COACHING,
            active=None,
            previous=None,
            rule=None,
            reason=reason,
            progress=PROGRESS_UNKNOWN,
            provisional=False,
            notes=notes,
            delivered=False,
            next_focus=None,
        )

    def _reference_advance(
        self,
        assessment: VisualAssessmentResult,
        state: CoachingSessionState,
        focus: str,
        progress: str,
        feedback_kind: str,
        ranking_status: str,
        primary: str | None,
        notes: tuple[str, ...],
        correction_for_primary: str | None,
    ) -> SessionCoachingDecision:
        if ranking_status == "unique_primary" and primary and primary != focus:
            return self._record(
                assessment,
                state,
                decision_type=ADVANCE_FOCUS,
                active=primary,
                previous=focus,
                rule=(
                    _cue_rule(correction_for_primary)
                    if correction_for_primary
                    else None
                ),
                reason="focus met the session mastered count",
                progress=progress,
                provisional=False,
                notes=notes,
                delivered=correction_for_primary is not None,
                selection_reason="focus met the session mastered count",
                feedback_kind=feedback_kind,
                next_focus=None,
            )
        if ranking_status == "tied":
            reason = (
                "focus met the session mastered count and the remaining "
                "deviations are tied"
            )
        else:
            reason = (
                "focus met the session mastered count and no other deviation "
                "is a unique primary"
            )
        return self._record(
            assessment,
            state,
            decision_type=NO_COACHING,
            active=None,
            previous=focus,
            rule=None,
            reason=reason,
            progress=progress,
            provisional=False,
            notes=notes,
            delivered=False,
            clear_focus=True,
            feedback_kind=feedback_kind,
            next_focus=None,
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
        next_focus: str | None | _Missing = _MISSING,
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
        candidate_id = None if isinstance(next_focus, _Missing) else next_focus
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
            candidate_next_focus=candidate_id,
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


def _weakness(
    assessment: VisualAssessmentResult, focus: str | None
) -> AssessmentFinding | None:
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


def _reference_assessment(
    shot_id: str,
    sequence: int | None,
    active: str | None,
    disposition: str,
    observation: str,
    frames: tuple[FrameReference, ...],
    *,
    failed: bool,
) -> VisualAssessmentResult:
    follow = None
    if active and not failed:
        if disposition == "execution":
            current = DESIRED_MOVEMENT_VISIBLE
            progress = FOLLOW_UP_IMPROVED
        elif disposition == "deviation":
            current = FAULT_STILL_VISIBLE
            progress = FOLLOW_UP_NOT_IMPROVED
        else:
            current = FOCUS_NOT_EVALUABLE
            progress = FOLLOW_UP_NOT_EVALUABLE
        follow = FocusFollowUp(
            previous_focus_id=active,
            current_focus_status=current,
            change_since_previous=CHANGE_UNKNOWN,
            progress_assessment=progress,
            current_focus_observation=observation,
            evidence_frames=tuple(frames),
            previous_image_count=0,
            stage_correspondence=STAGE_CORRESPONDENCE_UNAVAILABLE,
        )
    return VisualAssessmentResult(
        schema_version="1",
        shot_id=shot_id,
        status=STATUS_FAILED if failed else STATUS_OK,
        provenance=Provenance(
            "visual_assessment", "swing_reference", "2.9-timing-first"
        ),
        findings=(),
        no_proven_finding=True,
        stage_result_schema_version="1",
        stage_uncertainties=(),
        sequence=sequence,
        focus_follow_up=follow,
    )


def _session_counts(focus: str) -> CoachingRule:
    """Progress thresholds for one focus. Not a golf ranking."""

    return CoachingRule(
        finding_id=focus,
        kind="weakness",
        priority_rank=0,
        min_confidence=0.0,
        actionable=True,
        cue_id=focus,
        hud_text="session counts",
        speech_text="Session counts only.",
        possible_causes=(),
        contraindicated_while=(),
        observed_after=_SESSION_OBSERVED,
        confirmed_after=_SESSION_CONFIRMED,
        mastered_after=_SESSION_MASTERED,
        source="existing session continuity counts",
    )


def _cue_rule(rule_id: str) -> CoachingRule:
    """Identity of a selected reference rule. Player text is not stored here."""

    return CoachingRule(
        finding_id=rule_id,
        kind="weakness",
        priority_rank=0,
        min_confidence=0.0,
        actionable=True,
        cue_id=rule_id,
        hud_text="stored reference",
        speech_text="Stored in the reference model.",
        possible_causes=(),
        contraindicated_while=(),
        observed_after=_SESSION_OBSERVED,
        confirmed_after=_SESSION_CONFIRMED,
        mastered_after=_SESSION_MASTERED,
        source="reference rule id",
    )


def _tie_note(ranking: str, tied: tuple[str, ...]) -> tuple[str, ...]:
    if ranking != "tied" or not tied:
        return ()
    return (f"tied checkpoints: {', '.join(tied)}",)


def _kept_candidate(ranking: str, primary: str | None, focus: str | None) -> str | None:
    if ranking == "unique_primary" and primary and primary != focus:
        return primary
    return None
