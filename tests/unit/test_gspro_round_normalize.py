"""Turning a round-file entry into the shot CaddieJ owns.

Almost every test here is about something the conversion refuses to do. That is
the point of the module: the file has one number whose units are established
and several that would be useful if anybody knew what they meant, and the way
this goes wrong is by translating the second kind anyway.
"""

from datetime import UTC, datetime
from uuid import uuid4

from caddiej.adapters.gspro.round_normalize import shot_from_round
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.shot import Shot

NOW = datetime(2026, 9, 16, 10, 57, 23, tzinfo=UTC)
SESSION = uuid4()


def normalize(observed: ObservedRoundShot) -> Shot | None:
    return shot_from_round(observed, SESSION, NOW, 12.5)


def test_the_ball_speed_is_the_one_number_that_gets_translated() -> None:
    # The captured pair: the simulator showed 43.04 as 155 km/h, which is
    # 96.32 mph. Two real captures agreed on metres per second.
    shot = shot_from_round(
        ObservedRoundShot(shot_id="A", ball_speed_raw=43.03997039794922),
        SESSION,
        NOW,
        12.5,
    )

    assert shot is not None
    assert shot.ball is not None
    assert round(shot.ball.speed_mph) == 96


def test_no_launch_angle_and_no_start_line_are_invented() -> None:
    # The file has neither. Zeroes would be a shot that flew flat and dead
    # straight, and coaching would have two faults nothing measured.
    shot = shot_from_round(
        ObservedRoundShot(shot_id="A", ball_speed_raw=43.04), SESSION, NOW, 12.5
    )

    assert shot is not None
    assert shot.ball is not None
    assert shot.ball.launch_angle_degrees is None
    assert shot.ball.launch_direction_degrees is None


def test_the_distance_the_shot_went_is_not_recorded_as_its_carry() -> None:
    # TotalDistance includes run. Calling it carry would quietly wrong every
    # club distance derived from it.
    shot = shot_from_round(
        ObservedRoundShot(shot_id="A", ball_speed_raw=43.04, total_distance_raw=35.47),
        SESSION,
        NOW,
        12.5,
    )

    assert shot is not None
    assert shot.ball is not None
    assert shot.ball.carry_yards is None


def test_no_club_delivery_is_produced_from_a_ball_speed() -> None:
    # Face, path, attack angle and club speed are not in this file, and no
    # arrangement of a ball speed produces them.
    shot = shot_from_round(
        ObservedRoundShot(shot_id="A", ball_speed_raw=43.04), SESSION, NOW, 12.5
    )

    assert shot is not None
    assert shot.club_delivery is None
    assert shot.has_club_data is False


def test_the_shot_says_where_it_came_from() -> None:
    # So anything deciding what it may claim about a shot can tell "the face
    # was square" from "nobody measured the face".
    shot = shot_from_round(
        ObservedRoundShot(shot_id="A", ball_speed_raw=43.04), SESSION, NOW, 12.5
    )

    assert shot is not None
    assert shot.source.kind == "gspro_round"
    assert shot.source.device_id == "A"


def test_an_entry_with_no_ball_speed_is_not_a_shot() -> None:
    # A conceded putt, a recorded hole, or a partial write. Inventing a shot
    # for it would put a line on the floor about a swing nobody made.
    assert normalize(ObservedRoundShot(shot_id="A")) is None
    assert normalize(ObservedRoundShot(shot_id="B", ball_speed_raw=0.0)) is None


def test_the_same_round_shot_is_always_the_same_shot() -> None:
    # The file is re-read at startup. A shot that came back with a new identity
    # would be a second row in history, a second line on the floor and a second
    # call to a model.
    observed = ObservedRoundShot(shot_id="261ace26", ball_speed_raw=43.04)

    first = shot_from_round(observed, uuid4(), NOW, 1.0)
    second = shot_from_round(observed, uuid4(), NOW, 99.0)

    assert first is not None and second is not None
    assert first.shot_id == second.shot_id


def test_two_different_round_shots_are_two_different_shots() -> None:
    first = normalize(ObservedRoundShot(shot_id="A", ball_speed_raw=43.04))
    second = normalize(ObservedRoundShot(shot_id="B", ball_speed_raw=43.04))

    assert first is not None and second is not None
    assert first.shot_id != second.shot_id
