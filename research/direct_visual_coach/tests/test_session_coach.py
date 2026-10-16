"""Session coaching stays downstream of an assessment and off the image path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from contracts import (
    CONFIDENCE_SCALE,
    SCHEMA_VERSION,
    AssessmentFinding,
    FrameReference,
    Provenance,
    VisualAssessmentResult,
    to_plain,
)
from session_coach.assessment import validate_assessment
from session_coach.delivery import brief_for
from session_coach.engine import CoachingEngine
from session_coach.knowledge import check_record, draft_from_approved
from session_coach.models import (
    ADVANCE_FOCUS,
    CONTINUE_FOCUS,
    FOCUS_MASTERED,
    IMPROVEMENT_CONFIRMED,
    IMPROVEMENT_OBSERVED,
    NO_COACHING,
    PROGRESS_UNKNOWN,
    REASSESS_FOCUS,
    REINFORCE_PROGRESS,
    START_FOCUS,
    KnowledgeRecord,
    empty_state,
)
from session_coach.replay import load_state, replay, write_replay
from session_coach.rulebook import load_rulebook, rulebook_from_plain

RULES = (
    Path(__file__).resolve().parents[1] / "session_coach" / "rules_synthetic_v1.json"
)
CAUSE = "A grip change is one unverified possibility."


def _frame(tick: int = 37) -> FrameReference:
    return FrameReference(
        camera_id="swing_behind",
        relative_file=f"diagnostic/swing_behind/frame_{tick:03d}.jpg",
        capture_tick=tick,
        timestamp_seconds=float(tick) / 10,
    )


def _finding(
    fault_id: str | None,
    *,
    kind: str = "weakness",
    confidence: float = 0.8,
    comparison: str | None = None,
    unverified: bool = False,
    scale: str = CONFIDENCE_SCALE,
    frames: tuple[FrameReference, ...] | None = None,
) -> AssessmentFinding:
    return AssessmentFinding(
        kind=kind,
        fault_id=fault_id,
        confidence=confidence,
        confidence_scale=scale,
        evidence_frames=(_frame(),) if frames is None else frames,
        relied_on_stages=("DOWNSWING",),
        note="synthetic observation",
        comparison=comparison,
        unverified=unverified,
    )


def _assessment(
    shot_id: str,
    sequence: int,
    findings: tuple[AssessmentFinding, ...] = (),
    *,
    status: str = "ok",
    no_proven: bool | None = None,
    hypotheses: tuple[AssessmentFinding, ...] = (),
) -> VisualAssessmentResult:
    weaknesses = [item for item in findings if item.kind == "weakness"]
    return VisualAssessmentResult(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=status,
        provenance=Provenance("visual_assessment", "test", "1"),
        findings=findings,
        no_proven_finding=not weaknesses if no_proven is None else no_proven,
        stage_result_schema_version=SCHEMA_VERSION,
        stage_uncertainties=(),
        causal_hypotheses=hypotheses,
        sequence=sequence,
    )


def _run(shots: tuple[VisualAssessmentResult, ...], book=None):
    engine = CoachingEngine(book or load_rulebook(RULES))
    state = empty_state("session-a")
    decisions = []
    for shot in shots:
        decision = engine.decide(shot, state)
        decisions.append(decision)
        state = decision.resulting_state
    return decisions, state


def test_assessment_contract_rejects_a_weakness_marked_proven_absent() -> None:
    bad = _assessment(
        "s",
        1,
        (_finding("HAND_PATH_OUT"),),
        no_proven=True,
    )
    assert validate_assessment(bad)
    clean = _assessment("s", 1, no_proven=True)
    assert validate_assessment(clean) == ()
    causal = _finding("MAYBE_GRIP", kind="causal_hypothesis", unverified=False)
    assert validate_assessment(_assessment("s", 1, (causal,)))
    assert (
        validate_assessment(
            _assessment(
                "s",
                1,
                hypotheses=(
                    _finding("MAYBE_GRIP", kind="causal_hypothesis", unverified=True),
                ),
            )
        )
        == ()
    )


def test_first_focus_is_rule_rank_not_confidence() -> None:
    shot = _assessment(
        "s1",
        1,
        (
            _finding("EARLY_EXTENSION", confidence=0.99),
            _finding("HAND_PATH_OUT", confidence=0.55),
            _finding("NOT_ACTIONABLE", confidence=0.99),
        ),
    )
    decisions, state = _run((shot,))
    assert decisions[0].decision_type == START_FOCUS
    assert decisions[0].active_focus == "HAND_PATH_OUT"
    assert decisions[0].rule_id == "HAND_PATH_OUT"
    assert decisions[0].cue_id == "path_in"
    assert state.active_focus == "HAND_PATH_OUT"
    assert CAUSE not in decisions[0].reason
    assert "grip" not in decisions[0].uncertainty


def test_focus_persists_when_another_fault_scores_higher() -> None:
    first = _assessment("s1", 1, (_finding("HAND_PATH_OUT"),))
    second = _assessment(
        "s2",
        2,
        (
            _finding("HAND_PATH_OUT", confidence=0.4, comparison="still_supported"),
            _finding("SECOND_ISSUE", confidence=0.99),
        ),
    )
    decisions, state = _run((first, second))
    assert decisions[1].decision_type == CONTINUE_FOCUS
    assert decisions[1].progress == PROGRESS_UNKNOWN
    assert state.active_focus == "HAND_PATH_OUT"
    assert isinstance(state.active_focus, str)


def test_lower_confidence_without_a_comparison_is_not_improvement() -> None:
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT", confidence=0.9),)),
            _assessment("s2", 2, (_finding("HAND_PATH_OUT", confidence=0.2),)),
        )
    )
    assert decisions[1].decision_type == CONTINUE_FOCUS
    assert decisions[1].progress == PROGRESS_UNKNOWN
    assert state.progress == PROGRESS_UNKNOWN


def test_a_missing_finding_does_not_clear_the_focus() -> None:
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),
            _assessment("s2", 2, (_finding("SECOND_ISSUE"),)),
        )
    )
    assert decisions[1].decision_type == CONTINUE_FOCUS
    assert state.active_focus == "HAND_PATH_OUT"


def test_explicit_improvement_reinforces_then_confirms_then_advances() -> None:
    shots = [
        _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE"))),
    ]
    for index in range(2, 5):
        shots.append(
            _assessment(
                f"s{index}",
                index,
                (
                    _finding("HAND_PATH_OUT", comparison="improved"),
                    _finding("SECOND_ISSUE"),
                ),
            )
        )
    decisions, state = _run(tuple(shots))
    assert [item.decision_type for item in decisions] == [
        START_FOCUS,
        REINFORCE_PROGRESS,
        REINFORCE_PROGRESS,
        ADVANCE_FOCUS,
    ]
    assert [item.progress for item in decisions[1:]] == [
        IMPROVEMENT_OBSERVED,
        IMPROVEMENT_CONFIRMED,
        FOCUS_MASTERED,
    ]
    assert decisions[3].previous_focus == "HAND_PATH_OUT"
    assert state.active_focus == "SECOND_ISSUE"
    assert state.progress == PROGRESS_UNKNOWN


def test_contradiction_reassesses_and_does_not_keep_the_old_focus() -> None:
    decisions, state = _run(
        (
            _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE"))),
            _assessment(
                "s2",
                2,
                (
                    _finding("HAND_PATH_OUT", comparison="contradicted"),
                    _finding("SECOND_ISSUE"),
                ),
            ),
        )
    )
    assert decisions[1].decision_type == REASSESS_FOCUS
    assert state.active_focus == "SECOND_ISSUE"
    assert state.progress == PROGRESS_UNKNOWN


def test_no_coaching_when_nothing_is_eligible() -> None:
    causal = _finding("MAYBE", kind="causal_hypothesis", unverified=True)
    decisions, state = _run(
        (
            _assessment("s1", 1, no_proven=True),
            _assessment("s2", 2, (_finding("NOT_IN_BOOK"),)),
            _assessment("s3", 3, (causal,)),
            _assessment(
                "s4",
                4,
                (
                    _finding("HAND_PATH_OUT", kind="weakness"),
                    _finding("HAND_PATH_OUT", kind="strength"),
                ),
            ),
        )
    )
    assert [item.decision_type for item in decisions] == [NO_COACHING] * 4
    assert state.active_focus is None
    assert state.delivered_shot_ids == ()


def test_equal_ranks_are_provisional_and_alphabetical() -> None:
    book = rulebook_from_plain(
        {
            "rulebook_version": "tie-1",
            "approval_status": "synthetic_test_only",
            "rules": [
                _rule("BETA_ISSUE", 1, "beta_cue", "Work on beta"),
                _rule("ALPHA_ISSUE", 1, "alpha_cue", "Work on alpha"),
            ],
        }
    )
    decisions, _state = _run(
        (
            _assessment(
                "s1",
                1,
                (
                    _finding("BETA_ISSUE", confidence=0.99),
                    _finding("ALPHA_ISSUE", confidence=0.5),
                ),
            ),
        ),
        book,
    )
    assert decisions[0].active_focus == "ALPHA_ISSUE"
    assert decisions[0].provisional_priority is True


def test_duplicate_and_out_of_order_do_not_change_state() -> None:
    first = _assessment("s1", 1, (_finding("HAND_PATH_OUT"),))
    decisions, state = _run((first,))
    engine = CoachingEngine(load_rulebook(RULES))
    duplicate = engine.decide(first, state)
    late = engine.decide(_assessment("s0", 0, (_finding("SECOND_ISSUE"),)), state)
    assert duplicate.resulting_state is state
    assert late.resulting_state is state
    assert duplicate.errors == ("duplicate_shot",)
    assert late.errors == ("out_of_order",)


def test_a_failed_assessment_keeps_the_previous_state() -> None:
    decisions, state = _run((_assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),))
    engine = CoachingEngine(load_rulebook(RULES))
    failed = engine.decide(
        _assessment("s2", 2, (_finding("HAND_PATH_OUT"),), status="failed"),
        state,
    )
    broken = engine.decide(
        _assessment(
            "s2",
            2,
            (_finding("HAND_PATH_OUT", frames=()),),
        ),
        state,
    )
    assert failed.resulting_state is state
    assert broken.resulting_state is state
    assert failed.status == "failed"


def test_a_different_rulebook_version_is_refused() -> None:
    decisions, state = _run((_assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),))
    other = rulebook_from_plain(
        {
            "rulebook_version": "synthetic-2",
            "rules": [_rule("HAND_PATH_OUT", 1, "path_in", "Path more in")],
        }
    )
    decision = CoachingEngine(other).decide(
        _assessment("s2", 2, (_finding("HAND_PATH_OUT"),)),
        state,
    )
    assert decision.errors == ("stale_rulebook",)
    assert decision.resulting_state is state


def test_rulebook_rejects_duplicates_causes_and_bad_cues() -> None:
    with pytest.raises(ValueError):
        rulebook_from_plain(
            {
                "rulebook_version": "bad",
                "rules": [
                    _rule("SAME", 1, "cue_a", "Path more in"),
                    _rule("SAME", 2, "cue_b", "Stay in posture"),
                ],
            }
        )
    with pytest.raises(ValueError):
        rulebook_from_plain(
            {
                "rulebook_version": "bad",
                "rules": [
                    {
                        **_rule("SAME", 1, "cue_a", "Path more in"),
                        "possible_causes": [
                            {"statement": "seen", "status": "confirmed"}
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValueError):
        rulebook_from_plain(
            {
                "rulebook_version": "bad",
                "rules": [_rule("SAME", 1, "cue_a", "Too many words for the floor")],
            }
        )


def test_delivery_copies_the_rule_and_suppresses_no_coaching() -> None:
    book = load_rulebook(RULES)
    decisions, _state = _run((_assessment("s1", 1, (_finding("HAND_PATH_OUT"),)),))
    brief = brief_for(decisions[0], book)
    assert brief.presented is True
    assert brief.hud_text == "Path more in"
    assert brief.speech_text == "Keep working on the hand path."
    assert 2 <= len(brief.hud_text.split()) <= 5
    quiet = brief_for(
        CoachingEngine(book).decide(
            _assessment("empty", 1, no_proven=True), empty_state("s")
        ),
        book,
    )
    assert quiet.presented is False
    assert quiet.hud_text == ""
    assert CAUSE not in brief.speech_text


def test_session_round_trip_and_replay_are_deterministic(tmp_path: Path) -> None:
    shots = (
        _assessment("s1", 1, (_finding("HAND_PATH_OUT"), _finding("SECOND_ISSUE"))),
        _assessment(
            "s2", 2, (_finding("HAND_PATH_OUT", comparison="still_supported"),)
        ),
    )
    folder = tmp_path / "assessments"
    folder.mkdir()
    for shot in reversed(shots):
        (folder / f"{shot.shot_id}.json").write_text(
            json.dumps(to_plain(shot)), encoding="utf-8"
        )
    first, state = replay(shots, RULES, empty_state("replay"))
    second, again = replay(shots, RULES, empty_state("replay"))
    assert to_plain(first) == to_plain(second)
    assert to_plain(state) == to_plain(again)
    write_replay(first, state, tmp_path / "out")
    restored = load_state(tmp_path / "out" / "session_state.json")
    assert to_plain(restored) == to_plain(state)
    more, continued = replay(
        (_assessment("s3", 3, (_finding("HAND_PATH_OUT", comparison="improved"),)),),
        RULES,
        restored,
    )
    _full, full_state = replay(
        shots
        + (_assessment("s3", 3, (_finding("HAND_PATH_OUT", comparison="improved"),)),),
        RULES,
        empty_state("replay"),
    )
    assert more[0].decision_type == REINFORCE_PROGRESS
    assert to_plain(continued) == to_plain(full_state)
    ranked = rulebook_from_plain(
        {
            "rulebook_version": "other-rank",
            "rules": [
                _rule("SECOND_ISSUE", 1, "second_cue", "Next small change"),
                _rule("HAND_PATH_OUT", 5, "path_in", "Path more in"),
            ],
        }
    )
    other_path = tmp_path / "other.json"
    other_path.write_text(json.dumps(to_plain(ranked)), encoding="utf-8")
    changed, _changed_state = replay(shots, other_path, empty_state("replay"))
    assert changed[0].active_focus == "SECOND_ISSUE"
    assert first[0].active_focus == "HAND_PATH_OUT"


def test_unapproved_knowledge_cannot_become_a_rule() -> None:
    record = KnowledgeRecord(
        record_id="k1",
        source_video="coach-video-1",
        coach_id="coach-a",
        start_seconds=12.0,
        end_seconds=18.0,
        original_statement="The hands went out.",
        extracted_observation="Hands moved outward.",
        possible_cause="Grip.",
        suggested_correction="Path more in.",
        suggested_drill="",
        swing_stages=("DOWNSWING",),
        uncertainty="possible cause is unverified",
        review_status="unreviewed",
        contradictions=("k2",),
    )
    assert check_record(record) == ()
    assert draft_from_approved(record) is None
    approved = replace(record, review_status="approved")
    draft = draft_from_approved(approved)
    assert draft is not None
    assert draft["approval_status"] == "awaiting_rulebook_edit"
    book = load_rulebook(RULES)
    assert all(rule.finding_id != "k1" for rule in book.rules)


def test_coaching_modules_do_not_touch_images_or_production() -> None:
    root = Path(__file__).resolve().parents[1] / "session_coach"
    banned = ("openai", "cv2", "mediapipe", "kokoro", "fault_catalog", "caddiej")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text
    package = (root.parent / "evidence_package.py").read_text(encoding="utf-8")
    assert "session_coach" not in package


def _rule(finding_id: str, rank: int, cue_id: str, hud: str) -> dict:
    return {
        "finding_id": finding_id,
        "kind": "weakness",
        "priority_rank": rank,
        "min_confidence": 0.5,
        "actionable": True,
        "cue_id": cue_id,
        "hud_text": hud,
        "speech_text": "Keep this one change.",
        "possible_causes": [],
        "contraindicated_while": [],
        "observed_after": 1,
        "confirmed_after": 2,
        "mastered_after": 3,
        "source": "synthetic",
    }
