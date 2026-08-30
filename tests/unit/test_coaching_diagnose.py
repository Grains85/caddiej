"""Naming the fault: the part that has to be right whether or not a model answers.

Everything here is arithmetic over shots, so the tests are arithmetic too. The
cases that matter are the ones where a plausible implementation gets the golf
wrong: a left-handed player, a club whose ideal strike is the opposite of
another's, a player who is consistently bad, and a good shot.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from caddiej.coaching import Baseline, diagnose
from caddiej.coaching.diagnose import PREFERENCE_TOLERANCE, consistency_of
from caddiej.coaching.faults import IDEAL_ATTACK, oriented
from caddiej.domain.coaching import SEVERITY_FINE, CoachingFocus
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)

ShotFactory = Callable[..., Shot]


def a_shot(
    club: ClubKind = ClubKind.DRIVER,
    handedness: Handedness = Handedness.RIGHT,
    face: float | None = 0.0,
    path: float | None = 0.0,
    attack: float | None = None,
    strike: float | None = None,
    club_speed: float | None = None,
    launch: float = 12.6,
    spin: float | None = 2400.0,
    carry: float | None = 265.0,
    speed: float = 148.0,
) -> Shot:
    """A shot with everything at its ideal unless a test says otherwise."""

    if attack is None:
        attack = IDEAL_ATTACK.get(club, 0.0)
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=speed,
            launch_angle_degrees=launch,
            launch_direction_degrees=0.0,
            total_spin_rpm=spin,
            carry_yards=carry,
        ),
        club_delivery=ClubDelivery(
            speed_mph=club_speed,
            path_degrees=path,
            face_to_target_degrees=face,
            angle_of_attack_degrees=attack,
            impact_horizontal_mm=strike,
        ),
        handedness=handedness,
        club=Club(kind=club, vendor_code="XX"),
    )


def cold(club: ClubKind = ClubKind.DRIVER) -> object:
    """A baseline that knows nothing, which is how every session starts."""

    return Baseline().for_club(club)


# --- what a fault is ------------------------------------------------------


def test_a_shot_with_nothing_wrong_is_left_alone() -> None:
    # The most important case in the file. A coach who finds something wrong
    # with every swing is a coach nobody listens to, so "nothing to say" has to
    # be a real answer rather than the least bad fault.
    diagnosis = diagnose(a_shot(), cold())

    assert diagnosis.focus is CoachingFocus.NONE
    assert diagnosis.display == ""
    assert diagnosis.speech == ""
    assert not diagnosis.is_worth_saying


def test_an_open_face_is_named_and_scored() -> None:
    diagnosis = diagnose(a_shot(face=6.5), cold())

    assert diagnosis.focus is CoachingFocus.FACE_CONTROL
    assert diagnosis.display == "FACE OPEN"
    assert diagnosis.speech
    assert diagnosis.severity > SEVERITY_FINE


def test_a_closed_face_is_the_other_fault_and_the_other_instruction() -> None:
    open_face = diagnose(a_shot(face=6.5), cold())
    shut_face = diagnose(a_shot(face=-6.5), cold())

    assert shut_face.focus is CoachingFocus.FACE_CONTROL
    assert shut_face.display == "FACE CLOSED"
    # Same fault, opposite direction, so the same severity and a different fix.
    assert shut_face.severity == open_face.severity
    assert shut_face.speech != open_face.speech


def test_a_face_barely_off_square_is_not_worth_mentioning() -> None:
    # Inside the tolerance. Nobody hits it dead square, and saying so every
    # shot is how the floor becomes wallpaper.
    diagnosis = diagnose(a_shot(face=1.5), cold())

    assert diagnosis.focus is CoachingFocus.NONE


def test_the_worst_fault_wins_and_only_one_is_chosen() -> None:
    # A player can act on one idea per shot.
    diagnosis = diagnose(a_shot(face=7.5, path=4.0), cold())

    assert diagnosis.focus is CoachingFocus.FACE_CONTROL
    assert len(diagnosis.candidates) >= 2
    assert diagnosis.candidates[0].focus is CoachingFocus.FACE_CONTROL
    assert diagnosis.candidates[0].severity >= diagnosis.candidates[1].severity


def test_severity_never_leaves_its_scale() -> None:
    # A wild miss must not produce a severity of four, because the bands and the
    # floor's colours are defined on nought to one.
    diagnosis = diagnose(a_shot(face=45.0), cold())

    assert diagnosis.severity == 1.0
    assert diagnosis.focus is CoachingFocus.FACE_CONTROL


# --- handedness -----------------------------------------------------------


def test_an_open_face_is_open_for_a_left_handed_player_too() -> None:
    # The single most consequential piece of arithmetic here. Devices report the
    # angle in an absolute frame, so without mirroring a left-hander is told to
    # fix the opposite of what they did.
    right = diagnose(a_shot(face=6.5, handedness=Handedness.RIGHT), cold())
    left = diagnose(a_shot(face=-6.5, handedness=Handedness.LEFT), cold())

    assert right.display == "FACE OPEN"
    assert left.display == "FACE OPEN"
    assert left.speech == right.speech


def test_a_path_is_mirrored_and_a_strike_is_not() -> None:
    # A path is relative to the target, so it mirrors. The toe and the heel are
    # parts of the club, so they do not.
    left_path = diagnose(a_shot(path=-6.0, handedness=Handedness.LEFT), cold())
    right_path = diagnose(a_shot(path=6.0, handedness=Handedness.RIGHT), cold())
    assert left_path.display == right_path.display == "IN TO OUT"

    left_strike = diagnose(a_shot(strike=20.0, handedness=Handedness.LEFT), cold())
    right_strike = diagnose(a_shot(strike=20.0, handedness=Handedness.RIGHT), cold())
    assert left_strike.display == right_strike.display == "TOE STRIKE"


def test_unknown_handedness_is_treated_as_right_handed() -> None:
    # The common case and the only defensible guess.
    unknown = diagnose(a_shot(face=6.5, handedness=Handedness.UNKNOWN), cold())

    assert unknown.display == "FACE OPEN"
    assert oriented(6.5, Handedness.UNKNOWN) == 6.5


# --- the club changes what is right ---------------------------------------


def test_hitting_down_is_right_for_an_iron_and_wrong_for_a_driver() -> None:
    # Encoded because getting it wrong would tell a good iron player to stop
    # hitting down, which is the opposite of coaching.
    iron = diagnose(a_shot(club=ClubKind.IRON, attack=-4.0), cold(ClubKind.IRON))
    driver = diagnose(a_shot(club=ClubKind.DRIVER, attack=-4.0), cold())

    assert iron.focus is CoachingFocus.NONE
    assert driver.focus is CoachingFocus.LOW_POINT
    assert driver.display == "HITTING DOWN"


def test_a_putter_is_not_told_about_its_angle_of_attack() -> None:
    # No ideal for a putter, so the fault does not apply rather than being
    # scored against a number invented for it.
    diagnosis = diagnose(
        a_shot(club=ClubKind.PUTTER, attack=-6.0), cold(ClubKind.PUTTER)
    )

    focuses = {candidate.focus for candidate in diagnosis.candidates}
    assert CoachingFocus.LOW_POINT not in focuses


# --- what the player's own shots add --------------------------------------


def test_a_measurement_no_device_reported_is_not_a_fault() -> None:
    # Absent is not zero. A device that reports no club data at all must not
    # produce a shot with a perfect path and a perfect face.
    shot = a_shot(face=None, path=None, attack=None, strike=None)
    diagnosis = diagnose(shot, cold())

    focuses = {candidate.focus for candidate in diagnosis.candidates}
    assert CoachingFocus.FACE_CONTROL not in focuses
    assert CoachingFocus.PATH not in focuses


def test_a_shot_is_not_included_in_its_own_baseline() -> None:
    # Otherwise every first shot with a club is exactly average and nothing is
    # ever unusual.
    baseline = Baseline()
    shot = a_shot(club_speed=112.0)

    diagnosis = diagnose(shot, baseline.for_club(ClubKind.DRIVER))
    baseline.remember(shot)

    assert diagnosis.baseline_shots == 0
    assert baseline.for_club(ClubKind.DRIVER).shots == 1


def test_an_unusual_shot_is_caught_even_when_it_is_inside_the_fixed_band() -> None:
    # Club speed has no meaningful absolute band — a hundred miles an hour is
    # excellent with a driver and alarming with a nine iron — so it is only
    # rankable against the player's own.
    baseline = Baseline()
    for speed in (110.0, 111.0, 112.0, 111.5, 110.5):
        baseline.remember(a_shot(club_speed=speed))

    diagnosis = diagnose(a_shot(club_speed=95.0), baseline.for_club(ClubKind.DRIVER))

    assert diagnosis.focus is CoachingFocus.SPEED
    assert diagnosis.display == "CLUB SLOWER"


def test_a_player_who_is_always_open_still_gets_told() -> None:
    # The case a purely relative measure misses. Somebody who leaves the face
    # six degrees open on every swing has a mean of six and a tiny spread, so
    # nothing is ever unusual for them — and they still have a problem.
    baseline = Baseline()
    for face in (6.0, 6.2, 5.8, 6.1, 5.9):
        baseline.remember(a_shot(face=face))

    diagnosis = diagnose(a_shot(face=6.0), baseline.for_club(ClubKind.DRIVER))

    assert diagnosis.focus is CoachingFocus.FACE_CONTROL
    assert diagnosis.severity > SEVERITY_FINE


def test_a_baseline_mean_is_mirrored_with_the_measurement() -> None:
    # A left-hander whose normal is two degrees closed must not be described as
    # normally two degrees open.
    baseline = Baseline()
    for face in (2.0, 2.2, 1.8):
        baseline.remember(a_shot(face=face, handedness=Handedness.LEFT))

    diagnosis = diagnose(
        a_shot(face=-8.0, handedness=Handedness.LEFT),
        baseline.for_club(ClubKind.DRIVER),
    )
    observation = next(
        item
        for item in diagnosis.observations
        if item.focus is CoachingFocus.FACE_CONTROL
    )

    assert observation.label == "face open"
    assert observation.value == 8.0
    # Their usual, in their own terms: closed, so negative.
    assert observation.baseline_mean is not None
    assert observation.baseline_mean < 0


def test_two_identical_shots_do_not_make_the_third_infinitely_unusual() -> None:
    # A spread of zero means nothing is known about the spread, not that any
    # difference is infinite.
    baseline = Baseline()
    for _ in range(4):
        baseline.remember(a_shot(club_speed=110.0))

    diagnosis = diagnose(a_shot(club_speed=110.5), baseline.for_club(ClubKind.DRIVER))

    assert diagnosis.focus is CoachingFocus.NONE


def test_a_baseline_only_remembers_the_recent_past() -> None:
    baseline = Baseline(window=3)
    for speed in (80.0, 81.0, 110.0, 111.0, 112.0):
        baseline.remember(a_shot(club_speed=speed))

    summary = baseline.for_club(ClubKind.DRIVER).metrics["club_speed"]

    assert summary.count == 3
    assert summary.minimum == 110.0


def test_each_club_has_its_own_normal() -> None:
    baseline = Baseline()
    baseline.remember(a_shot(club=ClubKind.DRIVER, club_speed=112.0))
    baseline.remember(a_shot(club=ClubKind.WEDGE, club_speed=72.0))

    assert baseline.for_club(ClubKind.DRIVER).metrics["club_speed"].mean == 112.0
    assert baseline.for_club(ClubKind.WEDGE).metrics["club_speed"].mean == 72.0


# --- scatter --------------------------------------------------------------


def test_scatter_needs_enough_shots_to_be_scatter() -> None:
    baseline = Baseline()
    for carry in (250.0, 200.0):
        baseline.remember(a_shot(carry=carry))

    assert consistency_of(baseline.for_club(ClubKind.DRIVER)) is None


def test_a_scattered_group_becomes_a_fault_of_its_own() -> None:
    baseline = Baseline()
    for carry in (200.0, 265.0, 230.0, 275.0, 210.0, 255.0):
        baseline.remember(a_shot(carry=carry))

    result = consistency_of(baseline.for_club(ClubKind.DRIVER))

    assert result is not None
    observation, severity = result
    assert observation.focus is CoachingFocus.CONSISTENCY
    assert severity > SEVERITY_FINE
    # Already a comparison against the player's own shots, so no mean of a
    # different unit is smuggled in beside it.
    assert observation.baseline_mean is None


def test_a_tight_group_is_not_a_fault() -> None:
    baseline = Baseline()
    for carry in (263.0, 265.0, 264.0, 266.0, 265.5, 264.5):
        baseline.remember(a_shot(carry=carry))

    result = consistency_of(baseline.for_club(ClubKind.DRIVER))

    assert result is not None
    _observation, severity = result
    assert severity == 0.0


# --- what the player came to work on --------------------------------------


def test_the_coaching_focus_is_held_to_when_nothing_is_much_worse() -> None:
    # Without this a session becomes a heat map of whatever went wrong last,
    # which is the opposite of a lesson.
    shot = a_shot(face=6.0, path=7.0)
    unguided = diagnose(shot, cold())
    guided = diagnose(shot, cold(), preferred=CoachingFocus.PATH)

    assert unguided.focus is CoachingFocus.FACE_CONTROL
    assert guided.focus is CoachingFocus.PATH


def test_something_markedly_worse_overrides_the_focus() -> None:
    diagnosis = diagnose(
        a_shot(face=30.0, path=3.4), cold(), preferred=CoachingFocus.PATH
    )

    assert diagnosis.focus is CoachingFocus.FACE_CONTROL


def test_a_focus_nothing_measured_is_simply_not_used() -> None:
    diagnosis = diagnose(
        a_shot(face=6.5), cold(), preferred=CoachingFocus.EARLY_EXTENSION
    )

    assert diagnosis.focus is CoachingFocus.FACE_CONTROL


def test_the_preference_tolerance_is_the_one_it_says_it_is() -> None:
    # Guards the constant rather than restating it: a change to it should be
    # deliberate, because it decides how often a lesson is interrupted.
    assert 0.0 < PREFERENCE_TOLERANCE < 0.5


# --- determinism ----------------------------------------------------------


def test_the_same_shot_always_gets_the_same_diagnosis() -> None:
    # The whole reason the deterministic tier is trustworthy. Also what makes
    # the cache key meaningful.
    shot = a_shot(face=6.5, path=4.0, strike=12.0)

    first = diagnose(shot, cold())
    second = diagnose(shot, cold())

    assert first == second


def test_equally_bad_faults_are_broken_the_same_way_every_time() -> None:
    # Two faults at identical severity must not depend on the order of the
    # fault table, or a refactor changes what players are told.
    diagnosis = diagnose(a_shot(face=6.0, path=7.0), cold())
    again = diagnose(a_shot(face=6.0, path=7.0), cold())

    assert [candidate.focus for candidate in diagnosis.candidates] == [
        candidate.focus for candidate in again.candidates
    ]


def test_observations_are_ordered_worst_first() -> None:
    diagnosis = diagnose(a_shot(face=7.0, path=4.0, strike=10.0), cold())

    order = [candidate.focus for candidate in diagnosis.candidates]
    assert [observation.focus for observation in diagnosis.observations] == order
