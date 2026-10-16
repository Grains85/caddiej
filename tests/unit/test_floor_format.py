"""HUD formatting for GSPro round-play numbers.

Conversions are presentation-only. Domain raw values are not rewritten here.
"""

from caddiej.domain.observation import ObservedRoundShot
from caddiej.floor.format import (
    format_ball_speed_kmh,
    format_remaining_distance_m,
    format_target_distance_m,
    format_total_distance_m,
)


def test_ball_speed_metres_per_second_becomes_integer_kmh() -> None:
    # Hitting-bay currentRound.dat: ~32.8386, shown as 118 km/h.
    assert format_ball_speed_kmh(32.8386) == "118 km/h"
    assert format_ball_speed_kmh(32.84) == "118 km/h"


def test_total_distance_is_shown_as_integer_metres() -> None:
    assert format_total_distance_m(28.0) == "28 m"
    assert format_total_distance_m(28.4) == "28 m"
    assert format_total_distance_m(28.5) == "29 m"


def test_missing_or_non_finite_values_are_omitted() -> None:
    assert format_ball_speed_kmh(None) is None
    assert format_total_distance_m(None) is None
    assert format_ball_speed_kmh(float("nan")) is None
    assert format_total_distance_m(float("inf")) is None


def test_target_distance_is_shown_as_integer_metres() -> None:
    # Hardware: Code 201 DistanceToTarget 289.4 matched DistanceToPin 289.443.
    assert format_target_distance_m(347.0) == "347 m"
    assert format_target_distance_m(289.4) == "289 m"
    assert format_target_distance_m(147.2) == "147 m"
    assert format_target_distance_m(0.0) is None
    assert format_target_distance_m(None) is None


def test_remaining_distance_is_what_was_left_after_the_shot() -> None:
    """Hole 1 of a real round, 2026-09-16. Each shot's `DistanceToPin` is what was
    left once *that* shot stopped, which the hole's arithmetic settles:

        158.306 left, a 5.871 m shot, 153.129 left  — gained 5.18 m
        153.129 left, a 175.267 m shot, 23.142 left — overshot by 22 m
    """

    assert format_remaining_distance_m(158.306) == "158 m"
    assert format_remaining_distance_m(153.129486) == "153 m"
    assert format_remaining_distance_m(23.1421013) == "23 m"
    # And the cross-check against the other source: ProTee logged Code 201
    # DistanceToTarget 9.0 two seconds after the shot whose DistanceToPin is this.
    assert format_remaining_distance_m(9.007874) == "9 m"


def test_an_unusable_remaining_distance_is_omitted_rather_than_shown_as_zero() -> None:
    """Absent, so the overlay hides that figure instead of drawing a dash under a
    label. Nothing has been observed for a holed shot, so nothing is asserted
    about what GSPro writes for one."""

    assert format_remaining_distance_m(None) is None
    assert format_remaining_distance_m(0.0) is None
    assert format_remaining_distance_m(-1.0) is None
    assert format_remaining_distance_m(float("nan")) is None


def test_formatting_does_not_change_the_raw_observation() -> None:
    shot = ObservedRoundShot(
        shot_id="D",
        ball_speed_raw=32.8386,
        total_distance_raw=28.2,
        distance_to_pin_raw=9.007874,
    )

    assert format_ball_speed_kmh(shot.ball_speed_raw) == "118 km/h"
    assert format_total_distance_m(shot.total_distance_raw) == "28 m"
    assert format_remaining_distance_m(shot.distance_to_pin_raw) == "9 m"
    assert shot.ball_speed_raw == 32.8386
    assert shot.total_distance_raw == 28.2
    assert shot.distance_to_pin_raw == 9.007874
