"""Which club a distance calls for.

Deterministic golf arithmetic over numbers somebody measured. No model is asked
and nothing here reads a launch monitor: the input is a target distance and a
set of carries, and the output is one of those carries or nothing at all.
"""

import pytest

from caddiej.domain.bag import (
    ClubCarry,
    ClubDistanceProfile,
    ClubRecommendation,
    StaticClubDistances,
    recommend_club,
)

# A plausible amateur bag, in metres of carry. Values chosen to make the
# arithmetic in these tests obvious rather than to describe anybody.
BAG = ClubDistanceProfile.from_carries(
    {
        "DR": 220.0,
        "3W": 195.0,
        "5W": 180.0,
        "4I": 165.0,
        "5I": 155.0,
        "6I": 145.0,
        "7I": 135.0,
        "8I": 123.0,
        "9I": 110.0,
        "PW": 98.0,
        "GW": 85.0,
        "SW": 70.0,
    }
)


class TestChoosingAClub:
    def test_it_picks_the_carry_nearest_the_target(self) -> None:
        recommendation = recommend_club(137.0, BAG)

        assert recommendation == ClubRecommendation(club="7I", expected_carry_m=135.0)

    def test_nearest_beats_longest_that_still_reaches(self) -> None:
        """131 m is closer to the 7I's 135 than to the 8I's 123, even though the
        8I is the last club that lands short of it."""

        recommendation = recommend_club(131.0, BAG)

        assert recommendation is not None
        assert recommendation.club == "7I"

    def test_a_target_past_the_longest_club_gets_the_longest_club(self) -> None:
        recommendation = recommend_club(400.0, BAG)

        assert recommendation is not None
        assert recommendation.club == "DR"

    def test_a_target_inside_the_shortest_club_gets_the_shortest_club(self) -> None:
        recommendation = recommend_club(12.0, BAG)

        assert recommendation is not None
        assert recommendation.club == "SW"

    def test_a_tie_takes_the_longer_club_every_time(self) -> None:
        """Exactly between two clubs is where a rule that is not written down
        starts returning whichever one the dictionary happened to yield first."""

        profile = ClubDistanceProfile.from_carries({"8I": 120.0, "7I": 140.0})

        for _ in range(5):
            recommendation = recommend_club(130.0, profile)
            assert recommendation is not None
            assert recommendation.club == "7I"

    def test_the_carry_it_decided_on_comes_back_with_it(self) -> None:
        recommendation = recommend_club(96.0, BAG)

        assert recommendation == ClubRecommendation(club="PW", expected_carry_m=98.0)


class TestRefusingToGuess:
    def test_no_target_is_no_recommendation(self) -> None:
        assert recommend_club(None, BAG) is None

    def test_an_empty_bag_is_no_recommendation(self) -> None:
        assert recommend_club(147.0, ClubDistanceProfile()) is None

    def test_a_bag_of_zeroes_is_an_empty_bag(self) -> None:
        profile = ClubDistanceProfile.from_carries({"7I": 0.0, "PW": 0.0})

        assert profile.is_empty is True
        assert recommend_club(147.0, profile) is None

    @pytest.mark.parametrize("target", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_target_that_is_not_a_distance_is_no_recommendation(
        self, target: float
    ) -> None:
        assert recommend_club(target, BAG) is None


class TestTheProfile:
    def test_clubs_not_in_the_bag_are_dropped_rather_than_rejected(self) -> None:
        profile = ClubDistanceProfile.from_carries(
            {"DR": 220.0, "3W": 0.0, "7I": 135.0}
        )

        assert [carry.label for carry in profile.carries] == ["DR", "7I"]

    def test_it_holds_its_clubs_longest_first_however_they_arrived(self) -> None:
        profile = ClubDistanceProfile.from_carries(
            {"PW": 98.0, "DR": 220.0, "7I": 135.0}
        )

        assert [carry.label for carry in profile.carries] == ["DR", "7I", "PW"]

    def test_a_club_named_twice_is_refused(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            ClubDistanceProfile(
                carries=(
                    ClubCarry(label="7I", carry_m=135.0),
                    ClubCarry(label="7I", carry_m=140.0),
                )
            )

    @pytest.mark.parametrize("carry", [0.0, -1.0, float("nan")])
    def test_a_carry_that_is_not_a_distance_is_refused(self, carry: float) -> None:
        with pytest.raises(ValueError, match="carry_m"):
            ClubCarry(label="7I", carry_m=carry)

    def test_a_club_with_no_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="label"):
            ClubCarry(label="  ", carry_m=135.0)


class TestTheProvider:
    """The seam that lets carries come from history later without anything above
    it changing. Today it hands back what configuration said."""

    def test_it_hands_back_the_profile_it_was_given(self) -> None:
        provider = StaticClubDistances(carries=BAG)

        assert provider.profile() is BAG

    def test_an_unconfigured_provider_is_an_empty_bag(self) -> None:
        assert StaticClubDistances().profile().is_empty is True
