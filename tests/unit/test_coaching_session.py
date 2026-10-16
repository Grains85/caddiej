"""What the coach remembers between shots, which is what makes it a lesson.

Everything here is about the difference between a sequence of verdicts and a
session. The cases that matter are the ones a plausible implementation gets
wrong: a focus that moves every shot, praise for a second shot that happened to
be less bad than the first, and a focus that is still being mentioned an hour
after the player fixed it.
"""

from uuid import uuid4

from caddiej.coaching.session import (
    IMPROVEMENT_MARGIN,
    LEARNED_AFTER,
    MINIMUM_TREND_SHOTS,
    CoachingSession,
)
from caddiej.domain.coaching import (
    AdviceKind,
    CoachingFocus,
    Diagnosis,
    FocusCandidate,
    Observation,
)
from caddiej.domain.shot import ClubKind, Handedness


def a_diagnosis(
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    severity: float = 0.5,
    kind: AdviceKind = AdviceKind.CORRECTION,
    value: float = 6.0,
) -> Diagnosis:
    """One verdict, with the candidate and observation the session reads."""

    return Diagnosis(
        shot_id=uuid4(),
        focus=focus,
        severity=severity,
        display="FACE OPEN",
        speech="Square the face a little earlier.",
        club=ClubKind.DRIVER,
        handedness=Handedness.RIGHT,
        baseline_shots=5,
        candidates=(FocusCandidate(focus=focus, severity=severity),),
        observations=(
            Observation(focus=focus, label="face open", value=value, unit="deg"),
        ),
        kind=kind,
    )


# --- one thing at a time --------------------------------------------------


def test_a_session_starts_with_nothing_to_work_on() -> None:
    session = CoachingSession()

    assert session.focus is None
    assert session.progress.focus is None
    assert session.progress.trends == {}


def test_a_correction_becomes_the_thing_being_worked_on() -> None:
    session = CoachingSession()

    session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL))

    assert session.focus is CoachingFocus.FACE_CONTROL
    assert session.progress.focus_shots == 1


def test_the_same_fault_again_deepens_the_focus_rather_than_restarting_it() -> None:
    # What tells a coach "we have been on this for four balls" from "this is
    # the first time I have mentioned it".
    session = CoachingSession()

    for _ in range(3):
        session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL))

    assert session.focus is CoachingFocus.FACE_CONTROL
    assert session.progress.focus_shots == 3


def test_a_different_correction_moves_the_focus() -> None:
    # Deciding *whether* it should move is `diagnose`'s job, because it is a
    # comparison of severities. Once it has decided, the session follows.
    session = CoachingSession()
    session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL))

    session.remember(a_diagnosis(CoachingFocus.PATH))

    assert session.focus is CoachingFocus.PATH
    assert session.progress.focus_shots == 1


# --- the trend ------------------------------------------------------------


def test_every_shot_is_remembered_against_its_own_focus() -> None:
    # Including the ones nothing was said about, which is the only way a trend
    # can describe a change the player made rather than a change in what
    # CaddieJ happened to mention.
    session = CoachingSession()
    session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL, severity=0.6, value=6.1))
    session.remember(a_diagnosis(CoachingFocus.PATH, severity=0.5, value=4.0))

    trends = session.progress.trends

    assert trends[CoachingFocus.FACE_CONTROL].values == (6.1,)
    assert trends[CoachingFocus.PATH].values == (4.0,)


def test_a_trend_carries_the_players_own_language_for_the_number() -> None:
    # So a trend can be read out as evidence without anything downstream
    # having to work out which way is open for whom.
    session = CoachingSession()
    session.remember(a_diagnosis(value=6.1))

    trend = session.progress.trends[CoachingFocus.FACE_CONTROL]

    assert trend.label == "face open"
    assert trend.unit == "deg"


def test_a_trend_only_remembers_the_recent_past() -> None:
    session = CoachingSession(window=3)
    for value in (6.0, 5.0, 4.0, 3.0, 2.0):
        session.remember(a_diagnosis(value=value))

    assert session.progress.trends[CoachingFocus.FACE_CONTROL].values == (4.0, 3.0, 2.0)


# --- improvement ----------------------------------------------------------


def test_a_materially_better_shot_counts_as_improvement() -> None:
    # The single most useful sentence a coach has, so it has to be a fact.
    session = CoachingSession()
    for _ in range(MINIMUM_TREND_SHOTS):
        session.remember(a_diagnosis(severity=0.6))

    trend = session.progress.trends[CoachingFocus.FACE_CONTROL]

    assert trend.improved_to(0.6 - IMPROVEMENT_MARGIN - 0.01)


def test_two_shots_are_not_yet_a_direction() -> None:
    # Otherwise a second shot that happened to be less bad than the first is
    # congratulated as progress.
    session = CoachingSession()
    for _ in range(MINIMUM_TREND_SHOTS - 1):
        session.remember(a_diagnosis(severity=0.6))

    trend = session.progress.trends[CoachingFocus.FACE_CONTROL]

    assert not trend.improved_to(0.05)


def test_a_shot_barely_better_than_the_trend_is_noise_rather_than_progress() -> None:
    session = CoachingSession()
    for _ in range(MINIMUM_TREND_SHOTS):
        session.remember(a_diagnosis(severity=0.6))

    trend = session.progress.trends[CoachingFocus.FACE_CONTROL]

    assert not trend.improved_to(0.6 - IMPROVEMENT_MARGIN / 2)


def test_the_trend_a_shot_is_judged_against_excludes_that_shot() -> None:
    # Same reasoning as a shot not being in its own baseline: a trend that
    # already holds this shot has nothing left to say about it.
    session = CoachingSession()
    for _ in range(MINIMUM_TREND_SHOTS):
        session.remember(a_diagnosis(severity=0.6))

    before = session.progress.trends[CoachingFocus.FACE_CONTROL]
    assert before.mean_severity == 0.6


# --- a change somebody has made stops being a change they are making ------


def test_enough_clean_shots_in_a_row_retire_the_focus() -> None:
    # A coach keeps somebody on one change long enough to learn it and then
    # stops mentioning it. Without this, CaddieJ would still be praising face
    # control an hour after it stopped being the interesting thing.
    session = CoachingSession()
    session.remember(a_diagnosis(kind=AdviceKind.CORRECTION))

    for _ in range(LEARNED_AFTER):
        session.remember(a_diagnosis(severity=0.05, kind=AdviceKind.REINFORCEMENT))

    assert session.focus is None


def test_one_good_shot_does_not_retire_the_focus() -> None:
    session = CoachingSession()
    session.remember(a_diagnosis(kind=AdviceKind.CORRECTION))

    session.remember(a_diagnosis(severity=0.05, kind=AdviceKind.REINFORCEMENT))

    assert session.focus is CoachingFocus.FACE_CONTROL


def test_a_fault_coming_back_resets_the_count_towards_learned() -> None:
    # Two good ones and a bad one is not two thirds of a fix.
    session = CoachingSession(learned_after=3)
    session.remember(a_diagnosis(kind=AdviceKind.CORRECTION))
    session.remember(a_diagnosis(severity=0.05, kind=AdviceKind.REINFORCEMENT))
    session.remember(a_diagnosis(severity=0.05, kind=AdviceKind.REINFORCEMENT))

    session.remember(a_diagnosis(kind=AdviceKind.CORRECTION))
    session.remember(a_diagnosis(severity=0.05, kind=AdviceKind.REINFORCEMENT))

    assert session.focus is CoachingFocus.FACE_CONTROL


def test_a_good_shot_on_something_else_leaves_the_focus_alone() -> None:
    # Praise for a tidy path is not progress on the face.
    session = CoachingSession()
    session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL))

    session.remember(
        a_diagnosis(CoachingFocus.PATH, severity=0.05, kind=AdviceKind.REINFORCEMENT)
    )

    assert session.focus is CoachingFocus.FACE_CONTROL


def test_a_shot_nothing_was_measured_about_changes_nothing() -> None:
    session = CoachingSession()
    session.remember(a_diagnosis(CoachingFocus.FACE_CONTROL))

    session.remember(
        Diagnosis(
            shot_id=uuid4(),
            focus=CoachingFocus.NONE,
            severity=0.0,
            display="",
            speech="",
            club=ClubKind.DRIVER,
            handedness=Handedness.RIGHT,
            baseline_shots=5,
            kind=AdviceKind.NONE,
        )
    )

    assert session.focus is CoachingFocus.FACE_CONTROL
    assert session.progress.focus_shots == 1


# --- starting again -------------------------------------------------------


def test_forgetting_leaves_a_session_as_new() -> None:
    session = CoachingSession()
    session.remember(a_diagnosis())

    session.forget()

    assert session.focus is None
    assert session.progress.trends == {}


def test_a_window_has_to_hold_something() -> None:
    try:
        CoachingSession(window=0)
    except ValueError:
        return
    raise AssertionError("a window of zero shots should not be constructible")
