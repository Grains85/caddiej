"""The previous-shot note, the assessment request, and how the engine reads both."""

from __future__ import annotations

import json
from pathlib import Path

from contracts import (
    CHANGE_IMPROVED,
    CHANGE_UNKNOWN,
    CONFIDENCE_SCALE,
    DESIRED_MOVEMENT_VISIBLE,
    FAULT_STILL_VISIBLE,
    FOLLOW_UP_IMPROVED,
    FOLLOW_UP_NOT_IMPROVED,
    FOLLOW_UP_UNCERTAIN,
    IMPACT_ESTIMATED,
    IMPACT_UNRESOLVED,
    OBSERVED_BALL_MOTION,
    SCHEMA_VERSION,
    STAGE_CORRESPONDENCE_RELIABLE,
    STAGE_CORRESPONDENCE_UNCERTAIN,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    UNSUPPORTED_ESTIMATE,
    AssessmentFinding,
    CameraClockComparison,
    EvidenceFrame,
    FocusFollowUp,
    FrameReference,
    HandExcursion,
    ImpactEstimate,
    ImpactObservation,
    Provenance,
    StageBoundary,
    StageParameters,
    SwingEvidencePackage,
    SwingStageResult,
    VisualAssessmentResult,
    to_plain,
)
from session_coach.context import (
    CONTEXT_FIELDS,
    MAX_OBSERVATION_CHARS,
    build_previous_context,
)
from session_coach.delivery import brief_for
from session_coach.engine import CoachingEngine
from session_coach.models import (
    ADVANCE_FOCUS,
    COMPARATIVE_IMPROVEMENT,
    CONTINUE_FOCUS,
    DELIVERED,
    DELIVERY_UNKNOWN,
    NOT_DELIVERED,
    POSITIVE_OBSERVATION,
    REASSESS_FOCUS,
    REINFORCE_PROGRESS,
    START_FOCUS,
    DeliveryBrief,
    empty_state,
)
from session_coach.previous_images import PreviousImageCandidate, select_previous_images
from session_coach.replay import replay_trace, write_replay
from session_coach.request_adapter import build_assessment_request
from session_coach.rulebook import load_rulebook, rulebook_from_plain

RULES = (
    Path(__file__).resolve().parents[1] / "session_coach" / "rules_synthetic_v1.json"
)


def test_first_shot_has_no_previous_context() -> None:
    book = load_rulebook(RULES)
    assert build_previous_context(empty_state("s"), book, None) is None


def test_context_carries_the_delivered_cue_and_observation() -> None:
    book = load_rulebook(RULES)
    shot = _assessment(
        "s1", 1, (_finding("HAND_PATH_OUT", note="Hands moved outward early"),)
    )
    decision = CoachingEngine(book).decide(shot, empty_state("s"))
    brief = brief_for(decision, book)
    context = build_previous_context(decision.resulting_state, book, shot, brief)
    assert context is not None
    assert context.active_focus == "HAND_PATH_OUT"
    assert context.previous_cue == "Keep working on the hand path."
    assert context.previous_observation == "Hands moved outward early"
    assert context.delivery_status == DELIVERED
    assert set(to_plain(context)) == set(CONTEXT_FIELDS)


def test_an_undelivered_cue_is_not_described_as_given() -> None:
    book = load_rulebook(RULES)
    shot = _assessment(
        "s1", 1, (_finding("HAND_PATH_OUT", note="Hands moved outward early"),)
    )
    state = CoachingEngine(book).decide(shot, empty_state("s")).resulting_state
    quiet = DeliveryBrief("s1", START_FOCUS, False, "", "", "path_in", "no_coaching")
    context = build_previous_context(state, book, shot, quiet)
    assert context is not None
    assert context.previous_cue is None
    assert context.delivery_status == NOT_DELIVERED
    unknown = build_previous_context(state, book, shot, None)
    assert unknown is not None
    assert unknown.previous_cue is None
    assert unknown.delivery_status == DELIVERY_UNKNOWN


def test_context_ignores_an_unsupported_observation_and_stays_small() -> None:
    book = load_rulebook(RULES)
    causal = _finding(
        "HAND_PATH_OUT", kind="causal_hypothesis", unverified=True, note="Grip."
    )
    shot = _assessment("s1", 1, (_finding("HAND_PATH_OUT", note="x" * 400), causal))
    state = CoachingEngine(book).decide(shot, empty_state("s")).resulting_state
    context = build_previous_context(
        state,
        book,
        shot,
        brief_for(CoachingEngine(book).decide(shot, empty_state("s")), book),
    )
    assert context is not None
    assert context.previous_observation is not None
    assert len(context.previous_observation) == MAX_OBSERVATION_CHARS
    assert "Grip." not in context.previous_observation
    hypothesis = _assessment("s9", 9, (causal,))
    assert (
        build_previous_context(state, book, hypothesis, None).previous_observation
        is None
    )
    wrong = DeliveryBrief(
        "s1",
        START_FOCUS,
        True,
        "Much better now",
        "Much better than your last swing!",
        "path_in",
        None,
    )
    spoken = build_previous_context(state, book, shot, wrong)
    assert spoken is not None
    assert spoken.previous_cue == "Keep working on the hand path."
    assert "better" not in (spoken.previous_cue or "")
    assert len(json.dumps(to_plain(spoken))) < 800


def test_current_execution_reinforces_without_a_comparison_claim() -> None:
    book = load_rulebook(RULES)
    first = _assessment("s1", 1, (_finding("HAND_PATH_OUT"),))
    second = _assessment(
        "s2",
        2,
        (_finding("HAND_PATH_OUT"),),
        follow=_follow(DESIRED_MOVEMENT_VISIBLE, CHANGE_UNKNOWN, FOLLOW_UP_UNCERTAIN),
    )
    decisions, state = _run((first, second), book)
    assert decisions[1].decision_type == REINFORCE_PROGRESS
    assert decisions[1].feedback_kind == POSITIVE_OBSERVATION
    assert decisions[1].active_focus == "HAND_PATH_OUT"
    assert "requested movement" in decisions[1].reason
    assert "better" not in decisions[1].reason
    assert state.active_focus == "HAND_PATH_OUT"


def test_a_text_only_improvement_does_not_count_as_change() -> None:
    book = load_rulebook(RULES)
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),
            _assessment(
                "s2",
                2,
                (_finding("HAND_PATH_OUT"),),
                follow=_follow(
                    FAULT_STILL_VISIBLE,
                    CHANGE_IMPROVED,
                    FOLLOW_UP_IMPROVED,
                    images=0,
                ),
            ),
        ),
        book,
    )
    assert decisions[1].decision_type == CONTINUE_FOCUS
    assert decisions[1].feedback_kind == ""
    assert state.active_focus == "HAND_PATH_OUT"


def test_comparative_improvement_needs_previous_frames() -> None:
    book = load_rulebook(RULES)
    decisions, _state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE"))),
            _assessment(
                "s2",
                2,
                (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE")),
                follow=_follow(
                    DESIRED_MOVEMENT_VISIBLE,
                    CHANGE_IMPROVED,
                    FOLLOW_UP_IMPROVED,
                    images=1,
                    stage=STAGE_CORRESPONDENCE_RELIABLE,
                ),
            ),
        ),
        book,
    )
    assert decisions[1].decision_type == REINFORCE_PROGRESS
    assert decisions[1].feedback_kind == COMPARATIVE_IMPROVEMENT
    assert decisions[1].active_focus == "HAND_PATH_OUT"
    assert "previous frames" in decisions[1].reason


def test_a_strength_does_not_replace_the_focus() -> None:
    book = load_rulebook(RULES)
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),
            _assessment(
                "s2",
                2,
                (
                    _finding("HAND_PATH_OUT"),
                    _finding(
                        "BALANCE",
                        kind="strength",
                        note="Good balance through the finish.",
                    ),
                ),
                follow=_follow(
                    FAULT_STILL_VISIBLE, CHANGE_UNKNOWN, FOLLOW_UP_NOT_IMPROVED
                ),
            ),
        ),
        book,
    )
    assert decisions[1].decision_type == CONTINUE_FOCUS
    assert decisions[1].feedback_kind == POSITIVE_OBSERVATION
    assert state.active_focus == "HAND_PATH_OUT"


def test_advancement_follows_the_rulebook_count() -> None:
    strict = rulebook_from_plain(
        {
            "rulebook_version": "counts",
            "rules": [
                _rule("HAND_PATH_OUT", mastered=9),
                _rule("SECOND_ISSUE", rank=3),
            ],
        }
    )
    shots = [
        _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE")))
    ]
    for index in range(2, 5):
        shots.append(
            _assessment(
                f"s{index}",
                index,
                (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE")),
                follow=_follow(DESIRED_MOVEMENT_VISIBLE),
            )
        )
    held, held_state = _run(tuple(shots), strict)
    assert held[-1].decision_type == REINFORCE_PROGRESS
    assert held_state.active_focus == "HAND_PATH_OUT"
    advanced, advanced_state = _run(tuple(shots), load_rulebook(RULES))
    assert advanced[-1].decision_type == ADVANCE_FOCUS
    assert advanced[-1].rule_id == "SECOND_ISSUE"
    assert advanced_state.active_focus == "SECOND_ISSUE"


def test_supported_regression_reassesses() -> None:
    book = load_rulebook(RULES)
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE"))),
            _assessment(
                "s2",
                2,
                (_finding("SECOND_ISSUE"),),
                follow=_follow(
                    FAULT_STILL_VISIBLE,
                    "regressed",
                    FOLLOW_UP_NOT_IMPROVED,
                    images=2,
                    stage=STAGE_CORRESPONDENCE_RELIABLE,
                ),
            ),
        ),
        book,
    )
    assert decisions[1].decision_type == REASSESS_FOCUS
    assert state.active_focus == "SECOND_ISSUE"


def test_previous_images_stay_optional() -> None:
    frame = _frame()
    linked = PreviousImageCandidate(
        frame, STAGE_CORRESPONDENCE_RELIABLE, "HAND_PATH_OUT"
    )
    uncertain = PreviousImageCandidate(
        frame, STAGE_CORRESPONDENCE_UNCERTAIN, "HAND_PATH_OUT"
    )
    assert select_previous_images((linked,), requested=False, limit=2)[0] == ()
    kept, note = select_previous_images((uncertain,), requested=True, limit=2)
    assert kept == ()
    assert "not reliable" in note
    kept, _note = select_previous_images(
        (linked, linked, linked), requested=True, limit=5
    )
    assert len(kept) == 2


def test_request_uses_current_frames_and_optional_context() -> None:
    package = _package()
    request = build_assessment_request(package)
    assert request["previous_context"] is None
    assert request["previous_images"] == []
    assert "model" not in request
    assert "reasoning_effort" not in request
    frames = request["frames"]
    assert [item["capture_tick"] for item in frames] == [1, 2]
    assert frames[0]["relative_file"].endswith("frame_001.jpg")
    assert "stage" not in frames[0]
    assert "contact_interval_ticks" not in request["impact"]
    assert "current frames first" in request["instructions"].lower()
    assert "do not write a cue" in request["instructions"].lower()
    compared = build_assessment_request(
        package,
        previous=None,
        previous_images=(
            PreviousImageCandidate(
                _frame(9), STAGE_CORRESPONDENCE_UNCERTAIN, "HAND_PATH_OUT"
            ),
        ),
        compare_previous=True,
    )
    assert compared["previous_images"] == []
    assert len(compared["frames"]) == len(frames)


def test_replay_shows_what_the_next_assessment_would_know(tmp_path: Path) -> None:
    book_path = RULES
    shots = (
        _assessment(
            "s1", 1, (_finding("HAND_PATH_OUT", note="Hands moved outward early"),)
        ),
        _assessment(
            "s2",
            2,
            (_finding("HAND_PATH_OUT"),),
            follow=_follow(DESIRED_MOVEMENT_VISIBLE),
        ),
    )
    book = load_rulebook(book_path)
    first = CoachingEngine(book).decide(shots[0], empty_state("replay"))
    deliveries = {shots[0].shot_id: brief_for(first, book)}
    steps, _state = replay_trace(
        shots, book_path, empty_state("replay"), deliveries=deliveries
    )
    again, _again_state = replay_trace(
        shots, book_path, empty_state("replay"), deliveries=deliveries
    )
    assert to_plain(steps) == to_plain(again)
    assert steps[0].previous_context is None
    assert steps[0].next_context is not None
    assert steps[0].next_context.previous_cue == "Keep working on the hand path."
    assert steps[1].previous_context == steps[0].next_context
    assert steps[1].decision.decision_type == REINFORCE_PROGRESS
    assert steps[1].decision.rule_id == "HAND_PATH_OUT"
    missing, _missing_state = replay_trace(shots, book_path, empty_state("replay"))
    assert missing[0].next_context is not None
    assert missing[0].next_context.delivery_status == DELIVERY_UNKNOWN
    assert missing[0].next_context.previous_cue is None
    write_replay(
        tuple(step.decision for step in steps),
        steps[-1].decision.resulting_state,
        tmp_path,
        steps,
    )
    trace = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert trace[0]["previous_context"] is None
    assert trace[1]["previous_context"]["active_focus"] == "HAND_PATH_OUT"
    assert trace[1]["decision"]["reason"]


def _run(shots, book):
    engine = CoachingEngine(book)
    state = empty_state("session-a")
    decisions = []
    for shot in shots:
        decision = engine.decide(shot, state)
        decisions.append(decision)
        state = decision.resulting_state
    return decisions, state


def _frame(tick: int = 37) -> FrameReference:
    return FrameReference(
        "swing_behind",
        f"diagnostic/swing_behind/frame_{tick:03d}.jpg",
        tick,
        tick / 10,
    )


def _finding(
    fault_id: str,
    *,
    kind: str = "weakness",
    confidence: float = 0.8,
    note: str = "synthetic observation",
    frames: tuple[FrameReference, ...] | None = None,
    unverified: bool = False,
) -> AssessmentFinding:
    return AssessmentFinding(
        kind=kind,
        fault_id=fault_id,
        confidence=confidence,
        confidence_scale=CONFIDENCE_SCALE,
        evidence_frames=(_frame(),) if frames is None else frames,
        relied_on_stages=("DOWNSWING",),
        note=note,
        unverified=unverified,
    )


def _follow(
    status: str,
    change: str = CHANGE_UNKNOWN,
    progress: str = FOLLOW_UP_UNCERTAIN,
    *,
    images: int = 0,
    stage: str = "unavailable",
) -> FocusFollowUp:
    return FocusFollowUp(
        previous_focus_id="HAND_PATH_OUT",
        current_focus_status=status,
        change_since_previous=change,
        progress_assessment=progress,
        current_focus_observation="Hands stayed closer on this swing.",
        evidence_frames=(_frame(),),
        previous_image_count=images,
        stage_correspondence=stage,
    )


def _assessment(shot_id, sequence, findings, *, follow=None, status="ok"):
    weaknesses = [item for item in findings if item.kind == "weakness"]
    return VisualAssessmentResult(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=status,
        provenance=Provenance("visual_assessment", "test", "1"),
        findings=findings,
        no_proven_finding=not weaknesses,
        stage_result_schema_version=SCHEMA_VERSION,
        stage_uncertainties=(),
        sequence=sequence,
        focus_follow_up=follow,
    )


def _rule(finding_id: str, *, rank: int = 1, mastered: int = 3) -> dict:
    return {
        "finding_id": finding_id,
        "kind": "weakness",
        "priority_rank": rank,
        "min_confidence": 0.5,
        "actionable": True,
        "cue_id": "path_in" if finding_id == "HAND_PATH_OUT" else "second_cue",
        "hud_text": "Path more in"
        if finding_id == "HAND_PATH_OUT"
        else "Next small change",
        "speech_text": "Keep working on the hand path."
        if finding_id == "HAND_PATH_OUT"
        else "One later change is enough.",
        "possible_causes": [],
        "contraindicated_while": [],
        "observed_after": 1,
        "confirmed_after": 2,
        "mastered_after": mastered,
        "source": "synthetic",
    }


def _package() -> SwingEvidencePackage:
    frames = (
        EvidenceFrame(_frame(2), 1, "motion", "DOWNSWING"),
        EvidenceFrame(_frame(1), 0, "endpoint", "SETUP"),
    )
    stage = SwingStageResult(
        SCHEMA_VERSION,
        "s1",
        STATUS_FAILED,
        Provenance("swing_stage", "test", "1"),
        "swing_behind",
        "offset_seconds",
        "unknown",
        "unknown",
        CONFIDENCE_SCALE,
        StageParameters(*([0.0] * 10), 1, *([0.0] * 5)),
        (StageBoundary("DOWNSWING", (0.1, 0.2), 0.5, CONFIDENCE_SCALE, (2,), "test"),),
        (),
        ("IMPACT_REGION",),
        None,
        HandExcursion(False, None, None, None, None, None, False, False),
        ImpactObservation(False, "unsupported"),
    )
    return SwingEvidencePackage(
        schema_version=SCHEMA_VERSION,
        shot_id="s1",
        status=STATUS_OK,
        provenance=Provenance("evidence_package", "test", "1"),
        cameras=("swing_behind",),
        image_budget=24,
        selected_frames=frames,
        stage_estimates=(stage,),
        impact_estimate=ImpactEstimate(
            SCHEMA_VERSION,
            "s1",
            IMPACT_UNRESOLVED,
            Provenance("impact_estimation", "test", "1"),
            UNSUPPORTED_ESTIMATE,
            "ball_not_visible",
            None,
            CONFIDENCE_SCALE,
            "local_ball_appearance_change",
            "swing_behind",
            None,
            None,
        ),
        synchronization=CameraClockComparison(0, None, None, False, "test"),
        fallback="generic_motion",
        raw_capture_preserved=True,
    )


def test_estimated_impact_is_copied_and_a_failed_stage_is_not() -> None:
    package = _package()
    estimated = ImpactEstimate(
        SCHEMA_VERSION,
        "s1",
        IMPACT_ESTIMATED,
        Provenance("impact_estimation", "test", "1"),
        OBSERVED_BALL_MOTION,
        "test",
        0.6,
        CONFIDENCE_SCALE,
        "local_ball_appearance_change",
        "swing_behind",
        (40, 41),
        (3.8, 3.9),
    )
    accepted = SwingStageResult(
        SCHEMA_VERSION,
        "s1",
        STATUS_PARTIAL,
        Provenance("swing_stage", "test", "1"),
        "swing_behind",
        "offset_seconds",
        "unknown",
        "unknown",
        CONFIDENCE_SCALE,
        StageParameters(*([0.0] * 10), 1, *([0.0] * 5)),
        (StageBoundary("DOWNSWING", (0.1, 0.2), 0.5, CONFIDENCE_SCALE, (2,), "test"),),
        (),
        ("SETUP",),
        None,
        HandExcursion(False, None, None, None, None, None, False, False),
        ImpactObservation(False, "unsupported"),
    )
    package = SwingEvidencePackage(
        schema_version=package.schema_version,
        shot_id=package.shot_id,
        status=package.status,
        provenance=package.provenance,
        cameras=package.cameras,
        image_budget=package.image_budget,
        selected_frames=package.selected_frames,
        stage_estimates=(accepted,),
        impact_estimate=estimated,
        synchronization=package.synchronization,
        fallback=package.fallback,
        raw_capture_preserved=True,
    )
    request = build_assessment_request(package)
    by_tick = {item["capture_tick"]: item for item in request["frames"]}
    assert by_tick[2]["stage"] == "DOWNSWING"
    assert "stage" not in by_tick[1]
    assert request["impact"]["contact_interval_ticks"] == (40, 41)
