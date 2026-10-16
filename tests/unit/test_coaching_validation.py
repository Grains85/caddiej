"""What CaddieJ will and will not put on a floor or in somebody's ear.

This is the boundary where untrusted text becomes an `Advice`, so the tests are
mostly attempts to get something past it: a fault that was never measured, a
sentence with two instructions, a number that would contradict the readout beside
it, markup, a severity that overrules an instrument.

Every refusal has the same consequence — the templated line stands — which is why
being strict here is cheap.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.coaching.cues import (
    CUES_BY_ID,
    OPPOSITES,
    contradicting_words,
    cue_speech,
)
from caddiej.coaching.templates import display_for, legal_displays, speech_for
from caddiej.coaching.validate import (
    AdviceRejected,
    SpeechRejected,
    from_cue,
    from_diagnosis,
    recent_lines,
    validate,
)
from caddiej.domain.coaching import (
    AdviceKind,
    AdviceSource,
    CoachingFocus,
    Diagnosis,
    FocusCandidate,
    Observation,
    SeveritySource,
)
from caddiej.domain.cues import CueId
from caddiej.domain.shot import ClubKind, Handedness

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


def a_diagnosis(
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    severity: float = 0.42,
    label: str = "face open",
) -> Diagnosis:
    return Diagnosis(
        shot_id=uuid4(),
        focus=focus,
        severity=severity,
        display=label.upper(),
        speech="Square the face a little earlier.",
        club=ClubKind.DRIVER,
        handedness=Handedness.RIGHT,
        baseline_shots=12,
        candidates=(
            FocusCandidate(focus=focus, severity=severity),
            FocusCandidate(focus=CoachingFocus.PATH, severity=0.31),
        ),
        observations=(
            Observation(focus=focus, label=label, value=6.5, unit="deg"),
            Observation(
                focus=CoachingFocus.PATH, label="out to in", value=-4.0, unit="deg"
            ),
        ),
        kind=AdviceKind.CORRECTION,
        cue=CueId.FACE_OPEN,
        admissible_cues=(CueId.FACE_OPEN, CueId.PATH_OUT_TO_IN),
    )


def a_reply(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "cue": "FACE_OPEN",
        "speech": "Square the face a little earlier. Keep that same feeling.",
    }
    payload.update(overrides)
    return payload


HOLD_ANGLES = "Hold your angles through the ball."


def a_visual_diagnosis() -> Diagnosis:
    diagnosis = a_diagnosis()
    return Diagnosis(
        shot_id=diagnosis.shot_id,
        focus=diagnosis.focus,
        severity=diagnosis.severity,
        display=diagnosis.display,
        speech=diagnosis.speech,
        club=diagnosis.club,
        handedness=diagnosis.handedness,
        baseline_shots=diagnosis.baseline_shots,
        candidates=diagnosis.candidates,
        observations=diagnosis.observations,
        cue=diagnosis.cue,
        admissible_cues=(CueId.FACE_OPEN, CueId.EARLY_EXTENSION),
        kind=diagnosis.kind,
    )


# --- the reply that works -------------------------------------------------


def test_a_good_reply_becomes_an_advice() -> None:
    advice = validate(a_reply(), a_diagnosis(), NOW)

    assert advice.focus is CoachingFocus.FACE_CONTROL
    assert advice.display == "PASS THE TOE EARLY"
    assert advice.speech == "Square the face a little earlier. Keep that same feeling."
    assert advice.source is AdviceSource.MODEL
    assert advice.created_at == NOW


def test_an_affirmation_alone_is_not_coaching() -> None:
    # One instruction is the whole product. A sentence that only says well done
    # is pleasant and useless.
    with pytest.raises(AdviceRejected, match="instructions"):
        validate(
            a_reply(speech="Nice strike. Really solid contact."), a_diagnosis(), NOW
        )


def test_two_instructions_are_one_too_many() -> None:
    # A player can hold one idea over a ball.
    with pytest.raises(AdviceRejected, match="instructions"):
        validate(
            a_reply(speech="Square the face. Keep your head still."),
            a_diagnosis(),
            NOW,
        )


# --- the measurement wins -------------------------------------------------


def test_the_measured_severity_overrules_the_model() -> None:
    # Two severities for one shot is one too many. The instrument measured this
    # fault, so the colour on the floor and the stored row agree with it.
    diagnosis = a_diagnosis(severity=0.42)

    advice = validate(a_reply(severity=0.99), diagnosis, NOW)

    assert advice.severity == 0.42
    assert advice.severity_source is SeveritySource.MEASURED


def test_a_fault_the_shot_did_not_show_is_refused() -> None:
    # The model may choose among what was measured. Naming something else means
    # putting an unmeasured claim on the floor beside real numbers.
    with pytest.raises(AdviceRejected, match="not admissible"):
        validate(a_reply(cue="CURVE_SLICE"), a_diagnosis(), NOW)


def test_a_focus_only_a_video_shows_needs_a_video_to_have_been_sent() -> None:
    with pytest.raises(AdviceRejected, match="needs frames"):
        validate(a_reply(cue="EARLY_EXTENSION"), a_visual_diagnosis(), NOW)


def test_a_video_only_focus_is_allowed_once_frames_went_with_it() -> None:
    advice = validate(
        a_reply(cue="EARLY_EXTENSION", speech=HOLD_ANGLES, visual_confidence=0.4),
        a_visual_diagnosis(),
        NOW,
        frames_sent=True,
    )

    assert advice.focus is CoachingFocus.EARLY_EXTENSION
    assert advice.cue is CueId.EARLY_EXTENSION
    # Nothing measured this, so the model's impression is kept — but coarsened,
    # so a stored row cannot imply a precision that never existed.
    assert advice.severity_source is SeveritySource.OBSERVED
    assert advice.severity in {0.25, 0.5, 0.75}
    assert advice.confidence == 0.45


def test_a_video_only_focus_with_no_severity_is_refused() -> None:
    with pytest.raises(AdviceRejected, match="visual_confidence"):
        payload = a_reply(cue="EARLY_EXTENSION", speech=HOLD_ANGLES)
        payload.pop("visual_confidence", None)
        validate(payload, a_visual_diagnosis(), NOW, frames_sent=True)


def test_a_severity_outside_the_scale_is_refused() -> None:
    with pytest.raises(AdviceRejected, match="outside"):
        validate(
            a_reply(cue="EARLY_EXTENSION", speech=HOLD_ANGLES, visual_confidence=4.0),
            a_visual_diagnosis(),
            NOW,
            frames_sent=True,
        )


def test_calling_a_diagnosed_fault_nothing_is_refused() -> None:
    # A fault was measured. A model deciding otherwise would be overruling the
    # instrument with an opinion.
    diagnosis = a_diagnosis()
    diagnosis = Diagnosis(
        shot_id=diagnosis.shot_id,
        focus=diagnosis.focus,
        severity=diagnosis.severity,
        display=diagnosis.display,
        speech=diagnosis.speech,
        club=diagnosis.club,
        handedness=diagnosis.handedness,
        baseline_shots=diagnosis.baseline_shots,
        candidates=diagnosis.candidates,
        observations=diagnosis.observations,
        kind=AdviceKind.CORRECTION,
        cue=diagnosis.cue,
        admissible_cues=(*diagnosis.admissible_cues, CueId.FACE_CONTROL_GOOD),
    )
    with pytest.raises(AdviceRejected, match="reinforcement"):
        validate(a_reply(cue="FACE_CONTROL_GOOD"), diagnosis, NOW)


def test_a_focus_that_is_not_a_focus_at_all_is_refused() -> None:
    with pytest.raises(AdviceRejected, match="not a coaching cue"):
        validate(a_reply(cue="GRIP_PRESSURE"), a_diagnosis(), NOW)


# --- speech, as text that will be spoken and rendered ---------------------


def test_a_sentence_stating_a_number_is_refused() -> None:
    # The measurements are already on the floor. A model saying "about three
    # degrees" beside a readout of 6.5 has produced a contradiction, and the
    # player will believe the sentence.
    with pytest.raises(AdviceRejected, match="number"):
        validate(a_reply(speech="Square the face about 3 degrees."), a_diagnosis(), NOW)


def test_a_sentence_that_runs_on_is_refused() -> None:
    long = (
        "Square the face a little earlier through the ball and keep your "
        "balance all the way to a full and patient finish that you hold "
        "until the ball lands."
    )
    with pytest.raises(AdviceRejected, match="characters|words"):
        validate(a_reply(speech=long), a_diagnosis(), NOW)


def test_two_ideas_joined_by_punctuation_are_refused() -> None:
    with pytest.raises(AdviceRejected, match="';'"):
        validate(a_reply(speech="Square the face; keep still."), a_diagnosis(), NOW)


def test_markup_never_reaches_the_floor() -> None:
    # This text is assigned into a browser. It is length capped and reduced to
    # an explicit character set here, at the boundary, rather than trusted to
    # whatever renders it.
    with pytest.raises(AdviceRejected, match="characters the floor"):
        validate(a_reply(speech="Square the face <b>now</b>."), a_diagnosis(), NOW)


def test_an_empty_sentence_is_refused() -> None:
    with pytest.raises(AdviceRejected, match="empty"):
        validate(a_reply(speech="   "), a_diagnosis(), NOW)


def test_a_missing_sentence_is_refused() -> None:
    payload = a_reply()
    del payload["speech"]

    with pytest.raises(AdviceRejected, match="speech"):
        validate(payload, a_diagnosis(), NOW)


def test_whitespace_is_tidied_rather_than_being_a_reason_to_refuse() -> None:
    advice = validate(
        a_reply(
            speech="  Square the face a little earlier.   Keep that same feeling.  "
        ),
        a_diagnosis(),
        NOW,
    )

    assert advice.speech == "Square the face a little earlier. Keep that same feeling."


# --- the words agree with the cue they were written for --------------------


def test_a_face_open_cue_that_says_the_face_was_closed_is_refused() -> None:
    # A real reply. The cue was FACE_OPEN and the sentence said closed, and
    # George would have said both aloud. The cue stands; the words go.
    with pytest.raises(SpeechRejected, match="opposite of cue FACE_OPEN"):
        validate(
            a_reply(
                observation="Your face was closed at impact.",
                speech="Square the face a touch later through impact.",
            ),
            a_diagnosis(),
            NOW,
        )


def test_a_face_closed_cue_that_says_the_face_was_open_is_refused() -> None:
    diagnosis = replace(
        a_diagnosis(label="face closed"),
        cue=CueId.FACE_CLOSED,
        admissible_cues=(CueId.FACE_CLOSED,),
    )
    with pytest.raises(SpeechRejected, match="opposite of cue FACE_CLOSED"):
        validate(
            a_reply(
                cue="FACE_CLOSED",
                observation="Your face was open at impact.",
                speech="Hold the face a fraction longer.",
            ),
            diagnosis,
            NOW,
        )


def test_a_shut_face_is_a_closed_face() -> None:
    with pytest.raises(SpeechRejected, match="'shut'"):
        validate(
            a_reply(observation="The face was shut at impact."), a_diagnosis(), NOW
        )


def test_an_observation_inside_the_speech_is_held_to_the_cue_too() -> None:
    with pytest.raises(SpeechRejected, match="opposite"):
        validate(
            a_reply(speech="The face was closed. Square it a touch later."),
            a_diagnosis(),
            NOW,
        )


def test_a_correction_may_name_the_opposite_direction() -> None:
    # The fix for an open face is to close it. Only what was seen is checked.
    advice = validate(
        a_reply(
            observation="Your face was open at impact.",
            speech="Close the face through impact.",
        ),
        a_diagnosis(),
        NOW,
    )

    assert advice.speech == "Close the face through impact."


def test_a_path_correction_may_swing_from_the_inside() -> None:
    advice = validate(
        a_reply(
            cue="PATH_OUT_TO_IN",
            observation="Your path was out-to-in.",
            speech="Swing more from the inside.",
        ),
        a_diagnosis(),
        NOW,
    )

    assert advice.cue is CueId.PATH_OUT_TO_IN


def test_a_hyphenated_opposite_path_is_refused() -> None:
    with pytest.raises(SpeechRejected, match="'in to out'"):
        validate(
            a_reply(
                cue="PATH_OUT_TO_IN",
                observation="Your path was in-to-out.",
                speech="Swing more from the inside.",
            ),
            a_diagnosis(),
            NOW,
        )


def test_an_agreeing_observation_passes_unchanged() -> None:
    advice = validate(
        a_reply(observation="Your face was open at impact."), a_diagnosis(), NOW
    )

    assert advice.spoken_observation == "Your face was open at impact."


def test_a_cue_with_no_opposite_is_not_checked() -> None:
    assert contradicting_words(CueId.EARLY_EXTENSION) == frozenset()
    advice = validate(
        a_reply(
            cue="EARLY_EXTENSION",
            observation="You stood up and the face looked closed.",
            speech=HOLD_ANGLES,
            visual_confidence=0.4,
        ),
        a_visual_diagnosis(),
        NOW,
        frames_sent=True,
    )

    assert advice.cue is CueId.EARLY_EXTENSION


def test_the_opposite_is_matched_as_a_whole_word_in_any_case() -> None:
    with pytest.raises(SpeechRejected, match="'closed'"):
        validate(
            a_reply(observation="Your face was CLOSED at impact."), a_diagnosis(), NOW
        )
    advice = validate(
        a_reply(observation="Your face was open with an unclosed stance."),
        a_diagnosis(),
        NOW,
    )

    assert advice.cue is CueId.FACE_OPEN


def test_every_opposite_is_mutual_and_names_a_direction() -> None:
    for cue, opposite in OPPOSITES.items():
        assert OPPOSITES[opposite] is cue
        assert contradicting_words(cue)


# --- display, which is CaddieJ's own word for the fault -------------------


def test_a_display_outside_the_permitted_set_falls_back_to_the_template() -> None:
    # The one thing that is repaired rather than refused. The templated display
    # is already on the floor, so a freely worded one would replace a correct
    # statement about a measurement with a rewording of it, and flicker doing so.
    advice = validate(a_reply(display="YOUR FACE IS VERY OPEN"), a_diagnosis(), NOW)

    assert advice.display == "PASS THE TOE EARLY"


def test_a_one_word_display_falls_back() -> None:
    advice = validate(a_reply(display="OPEN"), a_diagnosis(), NOW)

    assert advice.display == "PASS THE TOE EARLY"


def test_a_display_with_a_number_in_it_falls_back() -> None:
    advice = validate(a_reply(display="FACE 6 OPEN"), a_diagnosis(), NOW)

    assert advice.display == "PASS THE TOE EARLY"


def test_a_lower_case_display_is_accepted_and_raised() -> None:
    advice = validate(a_reply(display="face open"), a_diagnosis(), NOW)

    assert advice.display == "PASS THE TOE EARLY"


# --- the fallback ---------------------------------------------------------


def test_the_templated_advice_is_what_the_player_was_already_reading() -> None:
    diagnosis = a_diagnosis()

    advice = from_diagnosis(diagnosis, NOW)

    assert advice.display == diagnosis.display
    assert advice.speech == diagnosis.speech
    assert advice.severity == diagnosis.severity
    assert advice.source is AdviceSource.TEMPLATE
    assert advice.shot_id == diagnosis.shot_id


def test_the_cue_lesson_is_the_registrys_reviewed_words_in_full() -> None:
    diagnosis = a_diagnosis()

    advice = from_cue(diagnosis, NOW)

    assert advice is not None
    spec = CUES_BY_ID[CueId.FACE_OPEN]
    assert advice.cue is CueId.FACE_OPEN
    assert advice.display == spec.display
    assert advice.observation == spec.observed
    assert advice.spoken_observation == spec.explanation
    assert advice.speech == cue_speech(CueId.FACE_OPEN)
    assert advice.source is AdviceSource.TEMPLATE
    assert advice.severity == diagnosis.severity


def test_the_cue_lesson_cites_only_a_frame_that_was_sent() -> None:
    diagnosis = a_diagnosis()

    cited = from_cue(
        diagnosis, NOW, frame_ids=("all_values",), evidence_frame_id="all_values"
    )
    unsent = from_cue(diagnosis, NOW, evidence_frame_id="all_values")

    assert cited is not None and cited.evidence_frame_id == "all_values"
    assert unsent is None


def test_no_cue_lesson_for_a_cue_this_shot_does_not_admit() -> None:
    diagnosis = replace(a_diagnosis(), admissible_cues=(CueId.PATH_OUT_TO_IN,))

    assert from_cue(diagnosis, NOW) is None
    assert from_cue(replace(a_diagnosis(), cue=None), NOW) is None


def test_validate_still_refuses_an_inadmissible_cue() -> None:
    diagnosis = replace(a_diagnosis(), admissible_cues=(CueId.FACE_OPEN,))

    with pytest.raises(AdviceRejected, match="PATH_OUT_TO_IN was not admissible"):
        validate(a_reply(cue="PATH_OUT_TO_IN"), diagnosis, NOW)


def test_every_templated_line_would_pass_its_own_validation() -> None:
    # The fallback has to be acceptable by the same rules as a model's answer,
    # or the safe path is the one that puts a bad line on the floor.
    for focus, (positive, negative) in _speech_table().items():
        if focus not in _CUES:
            continue
        for text in (positive, negative):
            payload = {
                "cue": _cue_for(focus).value,
                "speech": text,
            }
            diagnosis = _diagnosis_for(focus)
            advice = validate(payload, diagnosis, NOW, frames_sent=True)
            assert advice.speech == text


def test_recent_lines_are_the_last_few_things_said() -> None:
    spoken = [
        from_diagnosis(a_diagnosis(), NOW),
        from_diagnosis(a_diagnosis(), NOW),
        from_diagnosis(a_diagnosis(), NOW),
        from_diagnosis(a_diagnosis(), NOW),
    ]

    assert len(recent_lines(spoken)) == 3


def test_a_focus_only_a_video_shows_still_has_a_display() -> None:
    # Needed so a model that chose one produces something the floor can draw.
    for focus in (
        CoachingFocus.TEMPO,
        CoachingFocus.POSTURE,
        CoachingFocus.EARLY_EXTENSION,
        CoachingFocus.SWAY,
    ):
        display = display_for(focus)
        assert 2 <= len(display.split()) <= 4
        assert display in legal_displays(focus)


def _speech_table() -> dict[CoachingFocus, tuple[str, str]]:
    from caddiej.coaching.templates import SPEECH

    return dict(SPEECH)


_LABELS = {
    CoachingFocus.FACE_CONTROL: "face open",
    CoachingFocus.PATH: "in to out",
    CoachingFocus.STRIKE: "toe strike",
    CoachingFocus.LOW_POINT: "hitting up",
    CoachingFocus.SPEED: "club faster",
    CoachingFocus.START_LINE: "start pushed",
    CoachingFocus.CURVE: "slice spin",
    CoachingFocus.LAUNCH: "launch high",
    CoachingFocus.SPIN: "spin high",
    CoachingFocus.CONSISTENCY: "shots scattered",
}


def _label_for(focus: CoachingFocus) -> str:
    return _LABELS[focus]


def _diagnosis_for(focus: CoachingFocus) -> Diagnosis:
    label = _label_for(focus)
    return Diagnosis(
        shot_id=uuid4(),
        focus=focus,
        severity=0.5,
        display=display_for(focus, label),
        speech=speech_for(focus),
        club=ClubKind.IRON,
        handedness=Handedness.RIGHT,
        baseline_shots=6,
        candidates=(FocusCandidate(focus=focus, severity=0.5),),
        observations=(Observation(focus=focus, label=label, value=1.0, unit="deg"),),
        kind=AdviceKind.CORRECTION,
        cue=_cue_for(focus),
        admissible_cues=(_cue_for(focus),),
    )


_CUES = {
    CoachingFocus.FACE_CONTROL: CueId.FACE_OPEN,
    CoachingFocus.PATH: CueId.PATH_IN_TO_OUT,
    CoachingFocus.STRIKE: CueId.STRIKE_TOE,
    CoachingFocus.LOW_POINT: CueId.TOO_SHALLOW,
    CoachingFocus.SPEED: CueId.SPEED_RUSHED,
    CoachingFocus.START_LINE: CueId.START_PUSHED,
    CoachingFocus.CURVE: CueId.CURVE_SLICE,
    CoachingFocus.CONSISTENCY: CueId.SCATTER,
}


def _cue_for(focus: CoachingFocus) -> CueId:
    return _CUES[focus]
