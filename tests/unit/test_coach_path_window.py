"""A club path has a sign because a golf swing travels on an arc.

The shot that produced this file: an iron, struck well, club path 3.7 degrees
in-to-out, which the player had hit deliberately and reported as a good strike.
CaddieJ scored the path component 0.72 of its weight and told them, on the
floor, to SWING MORE ACROSS.

Two mechanisms combined to produce that, and both are tested here.

The path fault was centred on zero with a symmetric tolerance, so any sign at
all was a deviation to be ranked. And severity is the larger of the fixed band
and the distance from this player's own recent average, so a path that the
fixed band scored 0.10 came back as 0.82 for the separate reason that it was
unusual for them. Being unusual is not being wrong, and zero was never the
target: it is a shot shape, not a defect.

What this does not claim is that path is beyond criticism. A path far outside
its club's functional range is still a delivery problem and is still ranked --
by how far past the edge it went, which is the honest measurement.
"""

from __future__ import annotations

import pytest
from tests.unit.test_coaching_diagnose import a_shot, cold

from caddiej.coaching import Baseline, diagnose
from caddiej.coaching.faults import (
    FAULTS_BY_FOCUS,
    FUNCTIONAL_PATH,
    observe,
)
from caddiej.domain.coaching import SEVERITY_FINE, AdviceKind, CoachingFocus
from caddiej.domain.shot import ClubKind, Handedness

PATH = FAULTS_BY_FOCUS[CoachingFocus.PATH]

THE_DELIBERATE_DRAW = 3.7
"""Club path in degrees, in-to-out, from the shot that produced SWING MORE
ACROSS. Kept as a named constant because it is a measurement off a real bay
and not a number chosen to make a test pass."""


def a_player_who_usually_swings_straight(
    club: ClubKind = ClubKind.IRON,
) -> object:
    """A baseline where a modest in-to-out path is statistically unusual.

    The condition that mattered. With a tight recent average near zero, 3.7
    degrees is about two standard deviations out, and `relative_severity`
    ranked it 0.82 -- six times what the fixed band said. A functional window
    that the player's own scatter can overrule is not a functional window.
    """

    baseline = Baseline()
    for path in (-0.4, 0.3, -0.2, 0.5, 0.1, -0.3):
        baseline.remember(a_shot(club=club, path=path))
    return baseline.for_club(club)


# --- 1: a modest in-to-out iron path is not inherently corrective -----------


class TestAModestInToOutIronPathIsHealthy:
    def test_the_shot_that_was_told_to_swing_more_across_no_longer_is(self) -> None:
        diagnosis = diagnose(
            a_shot(club=ClubKind.IRON, path=THE_DELIBERATE_DRAW),
            a_player_who_usually_swings_straight(),
        )

        assert diagnosis.display != "SWING MORE ACROSS"
        assert diagnosis.focus is not CoachingFocus.PATH

    def test_it_is_recorded_as_healthy_rather_than_not_recorded(self) -> None:
        """The reading survives; only the severity is zero.

        A functional path is evidence, and evidence a player can be told about
        -- it is the difference between "CaddieJ saw a good path" and "CaddieJ
        did not look at the path". The observation keeps the number so the
        strip can print it and a reinforcement can name it.
        """

        measured = observe(
            PATH,
            a_shot(club=ClubKind.IRON, path=THE_DELIBERATE_DRAW),
            ClubKind.IRON,
            {},
        )

        assert measured is not None
        observation, severity = measured
        assert observation.value == pytest.approx(THE_DELIBERATE_DRAW)
        assert severity == 0.0

    def test_the_players_own_scatter_does_not_get_a_vote(self) -> None:
        """The precise override. This is what made 0.10 into 0.82."""

        baseline = a_player_who_usually_swings_straight()
        measured = observe(
            PATH,
            a_shot(club=ClubKind.IRON, path=THE_DELIBERATE_DRAW),
            ClubKind.IRON,
            baseline.metrics,  # type: ignore[attr-defined]
        )

        assert measured is not None
        _observation, severity = measured
        assert severity == 0.0

    def test_a_modest_out_to_in_iron_path_is_equally_not_a_fault(self) -> None:
        """The brief's other half: neither direction is a diagnosis by itself.

        Narrower than the in-to-out side, because out-to-in past a few degrees
        is the motion behind the common amateur miss. Two degrees is not.
        """

        measured = observe(
            PATH, a_shot(club=ClubKind.IRON, path=-2.0), ClubKind.IRON, {}
        )

        assert measured is not None
        _observation, severity = measured
        assert severity == 0.0


# --- 2: path=healthy cannot also generate a PATH correction -----------------


class TestAHealthyPathCannotAlsoBeACorrection:
    @pytest.mark.parametrize("path", [-3.0, -1.0, 0.0, 2.0, 4.0])
    def test_nothing_inside_the_iron_window_is_ever_corrective(
        self, path: float
    ) -> None:
        measured = observe(
            PATH, a_shot(club=ClubKind.IRON, path=path), ClubKind.IRON, {}
        )

        assert measured is not None
        _observation, severity = measured
        assert severity < SEVERITY_FINE

    def test_a_healthy_path_can_still_be_praised(self) -> None:
        """Zero severity is a reinforcement, not silence."""

        diagnosis = diagnose(
            a_shot(club=ClubKind.IRON, path=THE_DELIBERATE_DRAW), cold(ClubKind.IRON)
        )

        assert diagnosis.kind is AdviceKind.REINFORCEMENT


# --- 3: delivery direction does not fabricate a mechanical fault ------------


class TestExtremePathIsStillRankedFromTheEdge:
    def test_a_path_well_outside_the_window_is_a_delivery_problem(self) -> None:
        measured = observe(
            PATH, a_shot(club=ClubKind.IRON, path=9.0), ClubKind.IRON, {}
        )

        assert measured is not None
        _observation, severity = measured
        assert severity > 0.0

    def test_both_directions_can_leave_the_window(self) -> None:
        out = observe(PATH, a_shot(club=ClubKind.IRON, path=-9.0), ClubKind.IRON, {})
        into = observe(PATH, a_shot(club=ClubKind.IRON, path=9.0), ClubKind.IRON, {})

        assert out is not None and into is not None
        assert out[1] > 0.0
        assert into[1] > 0.0

    def test_the_more_extreme_path_is_the_more_severe_one(self) -> None:
        mild = observe(PATH, a_shot(club=ClubKind.IRON, path=6.0), ClubKind.IRON, {})
        wild = observe(PATH, a_shot(club=ClubKind.IRON, path=12.0), ClubKind.IRON, {})

        assert mild is not None and wild is not None
        assert wild[1] > mild[1]


# --- the windows themselves -------------------------------------------------


class TestTheWindowsAreCoherent:
    def test_every_full_swing_club_has_one(self) -> None:
        for club in (
            ClubKind.DRIVER,
            ClubKind.WOOD,
            ClubKind.HYBRID,
            ClubKind.IRON,
            ClubKind.WEDGE,
        ):
            assert club in FUNCTIONAL_PATH

    def test_the_putter_has_none_because_it_has_no_arc_worth_ranking(self) -> None:
        assert ClubKind.PUTTER not in FUNCTIONAL_PATH

    def test_zero_is_inside_every_window_without_being_its_centre(self) -> None:
        """Zero is functional. It is not the target, and this is how that is
        said in arithmetic: every window contains it, and no window is
        symmetric about it."""

        for club, (low, high) in FUNCTIONAL_PATH.items():
            assert low < 0.0 < high, club
            assert low != -high, club

    def test_the_long_clubs_allow_more_in_to_out_than_out_to_in(self) -> None:
        """How the clubs are actually used, not a preference for draws.

        Out-to-in past a few degrees with a long club is the motion behind the
        common amateur miss. In-to-out past a few degrees mostly produces a
        draw.
        """

        for club in (ClubKind.DRIVER, ClubKind.WOOD, ClubKind.HYBRID, ClubKind.IRON):
            low, high = FUNCTIONAL_PATH[club]
            assert high > abs(low), club

    def test_the_wedge_is_the_exception_and_skews_the_other_way(self) -> None:
        """A short club is delivered steeper and a little across.

        Telling a decent wedge player to stop doing that would be wrong, so
        the asymmetry above is a fact about long clubs rather than a house
        preference for one shot shape.
        """

        low, high = FUNCTIONAL_PATH[ClubKind.WEDGE]

        assert abs(low) > high

    def test_a_driver_tolerates_more_than_an_iron(self) -> None:
        """Struck after the low point, so the path is moving out by then."""

        assert FUNCTIONAL_PATH[ClubKind.DRIVER][1] > FUNCTIONAL_PATH[ClubKind.IRON][1]

    def test_a_window_is_not_consulted_for_a_fault_that_has_none(self) -> None:
        """Most faults have no reviewed healthy range and are ranked as before.

        A face angle has no window: open is open at every club, and an
        unusually open face for this player is worth saying something about.
        """

        face = FAULTS_BY_FOCUS[CoachingFocus.FACE_CONTROL]

        assert face.windows is None
        assert face.is_functional(ClubKind.IRON, 0.0) is False


class TestHandednessIsPreserved:
    def test_a_left_handers_draw_is_their_draw_and_not_their_slice(self) -> None:
        """The window is applied to the player-oriented reading.

        A device reports an absolute angle; `oriented` flips it for a
        left-hander. Applying a window to the raw number would call a
        left-hander's in-to-out path out-to-in, which is the one mistake that
        turns coaching into the opposite of coaching.
        """

        shot = a_shot(
            club=ClubKind.IRON,
            path=-THE_DELIBERATE_DRAW,
            handedness=Handedness.LEFT,
        )

        measured = observe(PATH, shot, ClubKind.IRON, {})

        assert measured is not None
        observation, severity = measured
        assert observation.label == "in to out"
        assert severity == 0.0
