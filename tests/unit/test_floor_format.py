"""HUD formatting for GSPro round-play numbers.

Conversions are presentation-only. Domain raw values are not rewritten here.
"""

from caddiej.domain.observation import ObservedRoundShot
from caddiej.floor.format import format_ball_speed_kmh, format_total_distance_m


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


def test_formatting_does_not_change_the_raw_observation() -> None:
    shot = ObservedRoundShot(
        shot_id="D", ball_speed_raw=32.8386, total_distance_raw=28.2
    )

    assert format_ball_speed_kmh(shot.ball_speed_raw) == "118 km/h"
    assert format_total_distance_m(shot.total_distance_raw) == "28 m"
    assert shot.ball_speed_raw == 32.8386
    assert shot.total_distance_raw == 28.2
