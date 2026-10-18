"""Session continuity uses the existing engine and stored corrections."""

from __future__ import annotations

from tests.unit.reference_support import (
    IRON,
    PACKAGE,
    SHOT_DIR,
    assessment,
    deviation,
    frame,
    reference,
    request_for,
)

from caddiej.swing_reference.pipeline import analyze_saved_swing
from caddiej.swing_reference.request import PlayerContext
from caddiej.swing_reference.session import decide, new_session
from caddiej.swing_reference.validate import (
    insufficient_assessment,
    validate_assessment,
)


def test_focus_stays_when_another_deviation_is_larger() -> None:
    book = reference()
    state = new_session("session")
    first_frame = frame(tick=23, seconds=2.7)
    first = _ready(
        book,
        "shot-1",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [first_frame]
            )
        },
        primary="takeaway.clubhead_position",
    )
    decision, delivery, note = decide(
        book, first, state, sequence=1, accepted=True, confirm_delivery=True
    )
    assert decision.decision_type == "START_FOCUS"
    assert decision.resulting_state.active_focus == "takeaway.clubhead_position"
    assert delivery.presented
    assert delivery.speech_text == _correction(
        book, "takeaway.clubhead_position", "inside"
    )
    assert delivery.speech_text != "Stored in the reference model."
    assert note is not None
    assert note.previous_cue == delivery.speech_text
    assert note.delivery_status == "delivered"

    later = frame(tick=36, seconds=3.6, stage="DOWNSWING")
    second = _ready(
        book,
        "shot-2",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [first_frame], severity="minor"
            ),
            "delivery.hip_depth": deviation(
                "delivery.hip_depth", "toward_ball", [later], severity="large"
            ),
        },
        primary="delivery.hip_depth",
    )
    decision, delivery, note = decide(
        book, second, decision.resulting_state, sequence=2, accepted=True
    )
    assert decision.decision_type == "CONTINUE_FOCUS"
    assert decision.resulting_state.active_focus == "takeaway.clubhead_position"
    assert "does not replace it" in decision.reason
    assert delivery.speech_text == _correction(
        book, "takeaway.clubhead_position", "inside"
    )
    assert note is not None
    assert note.active_focus == "takeaway.clubhead_position"


def test_positive_reinforcement_is_not_a_comparison() -> None:
    book = reference()
    state = new_session("session")
    evidence = frame()
    started, _delivery, _note = decide(
        book,
        _ready(
            book,
            "shot-1",
            {
                "takeaway.clubhead_position": deviation(
                    "takeaway.clubhead_position", "inside", [evidence]
                )
            },
            primary="takeaway.clubhead_position",
        ),
        state,
        sequence=1,
        accepted=True,
        confirm_delivery=True,
    )
    praised = _ready(
        book,
        "shot-2",
        {
            "takeaway.clubhead_position": {
                "status": "near_reference",
                "problem": None,
                "severity": None,
                "certainty": "high",
                "observation": "The clubhead stays in front of the hands.",
                "evidence_frames": _cite(evidence),
            }
        },
        ranking="no_supported_deviation",
        primary=None,
    )
    decision, delivery, note = decide(
        book,
        praised,
        started.resulting_state,
        sequence=2,
        accepted=True,
        confirm_delivery=True,
    )
    assert decision.decision_type == "REINFORCE_PROGRESS"
    assert decision.resulting_state.active_focus == "takeaway.clubhead_position"
    stored = book.checkpoints["takeaway.clubhead_position"].positive_feedback
    assert stored
    assert delivery.speech_text == stored
    assert "better than" not in delivery.speech_text.lower()
    assert decision.reason == "the current swing shows the requested movement"
    assert note is not None
    assert note.previous_cue == stored


def test_unconfirmed_delivery_does_not_claim_the_cue_was_heard() -> None:
    book = reference()
    evidence = frame()
    _decision, delivery, note = decide(
        book,
        _ready(
            book,
            "shot-1",
            {
                "takeaway.clubhead_position": deviation(
                    "takeaway.clubhead_position", "inside", [evidence]
                )
            },
            primary="takeaway.clubhead_position",
        ),
        new_session("session"),
        sequence=1,
        accepted=True,
    )
    assert delivery.presented
    assert note is not None
    assert note.previous_cue is None
    assert note.delivery_status == "not_delivered"
    assert note.active_focus == "takeaway.clubhead_position"


def _iron_deviation(book):
    evidence = frame()
    payload = assessment(
        book,
        "shot-context",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [evidence]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
    )
    return payload, evidence


def _applicability(errors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        error
        for error in errors
        if "applicability" in error or "context mismatch" in error
    )


def test_stock_iron_right_makes_the_iron_context_applicable() -> None:
    book = reference()
    payload, evidence = _iron_deviation(book)
    player = PlayerContext(
        club_type="iron",
        shot_type="stock",
        handedness="right",
        evidence_context="player_swing",
    )

    errors = validate_assessment(
        book, payload, request_for("shot-context", [evidence], context=player)
    )

    assert _applicability(errors) == ()


def test_a_missing_or_wrong_shot_type_rejects_an_iron_deviation() -> None:
    book = reference()
    payload, evidence = _iron_deviation(book)
    missing = PlayerContext(
        club_type="iron",
        handedness="right",
        evidence_context="player_swing",
    )
    wrong = PlayerContext(
        club_type="iron",
        shot_type="distance_wedge",
        handedness="right",
        evidence_context="player_swing",
    )

    missing_errors = validate_assessment(
        book, payload, request_for("shot-context", [evidence], context=missing)
    )
    wrong_errors = validate_assessment(
        book, payload, request_for("shot-context", [evidence], context=wrong)
    )

    assert any("does not establish applicability" in error for error in missing_errors)
    assert any(
        "known context mismatch" in error or "does not establish applicability" in error
        for error in wrong_errors
    )


def test_unknown_or_inapplicable_focus_is_not_success() -> None:
    book = reference()
    evidence = frame()
    started, _delivery, _note = decide(
        book,
        _ready(
            book,
            "shot-1",
            {
                "takeaway.clubhead_position": deviation(
                    "takeaway.clubhead_position", "inside", [evidence]
                )
            },
            primary="takeaway.clubhead_position",
        ),
        new_session("session"),
        sequence=1,
        accepted=True,
    )
    marks = len(started.resulting_state.progress_marks)
    unknown = insufficient_assessment(book, "shot-2", IRON.as_dict())
    assert validate_assessment(book, unknown, request_for("shot-2", [])) == ()
    held, silent, _note = decide(
        book, unknown, started.resulting_state, sequence=2, accepted=True
    )
    assert held.decision_type == "CONTINUE_FOCUS"
    assert "cannot be evaluated" in held.reason
    assert "not counted as success" in held.reason
    assert held.resulting_state.active_focus == "takeaway.clubhead_position"
    assert len(held.resulting_state.progress_marks) == marks
    assert silent.speech_text == ""

    driver = PlayerContext(
        club_type="driver",
        handedness="right",
        evidence_context="player_swing",
    )
    outside = insufficient_assessment(book, "shot-3", driver.as_dict())
    assert (
        validate_assessment(book, outside, request_for("shot-3", [], context=driver))
        == ()
    )
    kept, silent, _note = decide(
        book, outside, held.resulting_state, sequence=3, accepted=True
    )
    assert kept.decision_type == "CONTINUE_FOCUS"
    assert "outside the current player context" in kept.reason
    assert kept.resulting_state.active_focus == "takeaway.clubhead_position"
    assert len(kept.resulting_state.progress_marks) == marks
    assert silent.speech_text == ""


def test_ineligible_correction_is_not_spoken() -> None:
    book = reference()
    evidence = frame(stage="SETUP", tick=2, seconds=0.2)
    payload = _ready(
        book,
        "shot-1",
        {
            "setup.ball_distance": deviation(
                "setup.ball_distance", "too_far", [evidence]
            )
        },
        primary="setup.ball_distance",
    )
    decision, delivery, note = decide(
        book, payload, new_session("session"), sequence=1, accepted=True
    )
    assert decision.decision_type == "START_FOCUS"
    assert decision.resulting_state.active_focus == "setup.ball_distance"
    assert delivery.presented is False
    assert delivery.speech_text == ""
    assert note is not None
    assert note.previous_cue is None
    assert note.delivery_status == "not_delivered"


def test_tie_is_preserved() -> None:
    book = reference()
    backswing = frame()
    setup = frame(stage="SETUP", tick=2, seconds=0.2, sha="other")
    payload = _ready(
        book,
        "shot-tie",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [backswing], severity="clear"
            ),
            "setup.posture": deviation(
                "setup.posture", "rounded", [setup], severity="clear"
            ),
        },
        ranking="tied",
        primary=None,
        secondary=["takeaway.clubhead_position", "setup.posture"],
    )
    decision, delivery, _note = decide(
        book, payload, new_session("session"), sequence=1, accepted=True
    )
    assert decision.decision_type == "NO_COACHING"
    assert decision.resulting_state.active_focus is None
    assert delivery.presented is False
    assert "tied" in decision.reason


def test_failed_model_request_stays_silent() -> None:
    result = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response_error="model request failed: TimeoutError",
        sequence=1,
    )
    assert result.presented is False
    assert result.speech_text == ""
    assert result.hud_text == ""
    assert result.state.assessment_shot_ids == ()
    assert result.state.active_focus is None
    assert "TimeoutError" not in result.speech_text


def test_failed_and_duplicate_and_stale_shots_do_not_coach() -> None:
    book = reference()
    result = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response="not-json",
        sequence=1,
    )
    assert result.presented is False
    assert result.hud_text == ""
    assert result.decision_status == "failed"
    assert result.state.active_focus is None
    assert result.state.assessment_shot_ids == ()

    evidence = frame()
    first = _ready(
        book,
        "shot-1",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [evidence]
            )
        },
        primary="takeaway.clubhead_position",
    )
    decision, delivery, _note = decide(
        book, first, new_session("session"), sequence=1, accepted=True
    )
    assert delivery.presented
    duplicate, silent, _note = decide(
        book,
        {**first, "shot_id": "shot-1"},
        decision.resulting_state,
        sequence=2,
        accepted=True,
    )
    assert duplicate.status == "failed"
    assert silent.presented is False
    assert duplicate.resulting_state.active_focus == "takeaway.clubhead_position"
    stale, silent, _note = decide(
        book,
        {**first, "shot_id": "shot-0"},
        decision.resulting_state,
        sequence=1,
        accepted=True,
    )
    assert "out_of_order" in stale.errors
    assert silent.speech_text == ""


def test_malformed_model_text_never_reaches_the_player() -> None:
    book = reference()
    evidence = frame()
    rule = next(
        item
        for item in book.checkpoints["takeaway.clubhead_position"].rules
        if "inside" in item["when"]["problems"]
    )
    payload = _ready(
        book,
        "shot-1",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [evidence]
            )
        },
        primary="takeaway.clubhead_position",
        coaching={
            "checkpoint_id": "takeaway.clubhead_position",
            "rule_id": rule["id"],
            "feedback": "You are worse than last time.",
            "correction": "Invent a new move.",
        },
    )
    request = request_for("shot-1", [evidence])
    from caddiej.swing_reference.pipeline import _accept
    from caddiej.swing_reference.request import PreparedRequest

    prepared = PreparedRequest(
        shot_id="shot-1",
        reference_version=book.version,
        manifest={**request, "reference": book.raw},
        images=(),
        system="",
        instructions="",
        schema=book.schema,
        warnings=(),
        reference_prefix="",
    )
    result = _accept(
        prepared, new_session("session"), book, 1, payload, confirm_delivery=False
    )
    assert result.recovery == ("model coaching text discarded",)
    assert result.presented
    assert result.speech_text == rule["correction"]
    assert "Invent" not in result.speech_text
    assert "worse than" not in result.speech_text


def _ready(
    book,
    shot_id,
    updates,
    primary="takeaway.clubhead_position",
    ranking="unique_primary",
    secondary=None,
    coaching=None,
):
    return assessment(
        book,
        shot_id,
        updates,
        ranking=ranking,
        primary=primary,
        secondary=secondary,
        note="Supported by the attached frames.",
        coaching=coaching,
    )


def _cite(item: dict) -> list[dict]:
    return [
        {
            "camera_id": item["camera_id"],
            "relative_file": item["relative_file"],
            "capture_tick": item["capture_tick"],
            "timestamp_seconds": item["timestamp_seconds"],
        }
    ]


def _correction(book, checkpoint_id: str, problem: str) -> str:
    for rule in book.checkpoints[checkpoint_id].rules:
        if problem in rule["when"]["problems"] and not rule["when"].get("requires"):
            return str(rule["correction"])
    raise AssertionError(problem)
