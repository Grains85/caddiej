"""The approved correction is copied. An observation is optional and checked."""

from __future__ import annotations

from tests.unit.reference_support import frame, reference

from caddiej.swing_reference.feedback import (
    FeedbackBrief,
    comparison_supported,
    compose_feedback,
    uses_wording_call,
)
from caddiej.swing_reference.request import PreviousNote

_CHECKPOINT = "takeaway.clubhead_position"
_RULE = "takeaway.clubhead_position.keep_head_in_front"
_APPROVED = (
    "In the backswing, when the shaft first reaches horizontal, let the hands "
    "move inward with the body turn while the clubhead rises and stays over "
    "the hands or slightly toward the ball in the rear-view image."
)
_SEEN = "The clubhead is inside the hands."


def _brief(
    *,
    comparison: bool = False,
    observation: str = _SEEN,
) -> FeedbackBrief:
    return FeedbackBrief(
        checkpoint_id=_CHECKPOINT,
        rule_id=_RULE,
        approved_text=_APPROVED,
        observation=observation,
        previous_observation="The clubhead was inside.",
        previous_feedback="Keep the clubhead over the hands.",
        progress="CONTINUE_FOCUS",
        comparison_supported=comparison,
    )


def _draft(**overrides: str) -> dict[str, str]:
    payload = {
        "personal_observation": _SEEN,
        "spoken_feedback": "Keep your trail elbow tucked and swing slower.",
        "short_feedback": "Tuck the elbow",
        "referenced_rule_id": _RULE,
        "referenced_checkpoint_id": _CHECKPOINT,
    }
    payload.update(overrides)
    return payload


def test_exact_mode_speaks_the_stored_sentence_only() -> None:
    wording = compose_feedback(
        reference(), _brief(), _draft(), strategy="exact"
    )
    assert wording.source == "correction"
    assert wording.speech == _APPROVED
    assert wording.hud == _APPROVED
    assert wording.observation_included is False
    assert "Tuck the elbow" not in wording.speech
    assert not uses_wording_call("exact")


def test_a_checked_observation_is_prefixed_and_the_correction_is_exact() -> None:
    wording = compose_feedback(
        reference(), _brief(), None, strategy="observed"
    )
    assert wording.source == "observation"
    assert wording.speech == f"{_SEEN} {_APPROVED}"
    assert wording.hud == wording.speech
    assert wording.speech.endswith(_APPROVED)
    assert wording.observation_included is True


def test_a_wording_draft_cannot_replace_the_correction() -> None:
    wording = compose_feedback(
        reference(), _brief(observation=""), _draft(), strategy="worded"
    )
    assert wording.speech == f"{_SEEN} {_APPROVED}"
    assert "trail elbow" not in wording.speech
    assert "swing slower" not in wording.speech
    assert "Tuck the elbow" not in wording.hud
    assert uses_wording_call("worded")
    assert not uses_wording_call("observed")


def test_an_instruction_or_another_checkpoint_is_omitted() -> None:
    book = reference()
    instructed = compose_feedback(
        book,
        _brief(),
        _draft(personal_observation="Keep your trail elbow tucked."),
        strategy="worded",
    )
    assert instructed.speech == _APPROVED
    assert instructed.observation_included is False
    other = compose_feedback(
        book,
        _brief(),
        _draft(personal_observation="The hands are off at setup.grip."),
        strategy="worded",
    )
    assert other.speech == _APPROVED
    assert "setup.grip" not in other.speech


def test_a_wrong_rule_id_drops_the_observation() -> None:
    wording = compose_feedback(
        reference(),
        _brief(),
        _draft(referenced_rule_id="setup.grip.neutralize_weak"),
        strategy="worded",
    )
    assert wording.speech == _APPROVED
    assert wording.observation_included is False


def test_an_unsupported_comparison_is_omitted() -> None:
    wording = compose_feedback(
        reference(),
        _brief(comparison=False, observation="That was better than the last swing."),
        None,
        strategy="observed",
    )
    assert wording.speech == _APPROVED
    assert "better than" not in wording.speech


def test_a_supported_comparison_may_precede_the_correction() -> None:
    seen = "That was better than the last swing."
    wording = compose_feedback(
        reference(),
        _brief(comparison=True, observation=seen),
        None,
        strategy="observed",
    )
    assert wording.speech == f"{seen} {_APPROVED}"
    assert wording.speech.endswith(_APPROVED)


def test_comparison_needs_a_recorded_deviation_becoming_smaller() -> None:
    evidence = [frame()]
    finding = {
        "status": "near_reference",
        "severity": None,
        "evidence_frames": evidence,
    }
    previous = PreviousNote(
        active_focus=_CHECKPOINT,
        previous_cue="Keep the clubhead over the hands.",
        previous_observation="The clubhead was inside the hands.",
        delivery_status="delivered",
        previous_status="deviation",
        previous_severity="clear",
    )
    assert comparison_supported(previous, finding, _CHECKPOINT) is True
    assert comparison_supported(None, finding, _CHECKPOINT) is False
    undelivered = PreviousNote(
        active_focus=_CHECKPOINT,
        previous_cue=None,
        previous_observation="The clubhead was inside the hands.",
        delivery_status="not_delivered",
        previous_status="deviation",
        previous_severity="clear",
    )
    assert comparison_supported(undelivered, finding, _CHECKPOINT) is False
