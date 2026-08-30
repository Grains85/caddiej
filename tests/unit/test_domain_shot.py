"""The normalized shot models.

These are CaddieJ's own vocabulary, so the tests are about what the domain
refuses to represent as much as what it holds.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Detection,
    Handedness,
    Shot,
    ShotSource,
)


def source(**overrides: object) -> ShotSource:
    values: dict[str, object] = {
        "kind": "gspro_connect",
        "device_session_id": uuid4(),
        "observed_at": datetime.now(UTC),
        "observed_monotonic": 1234.5,
    }
    values.update(overrides)
    return ShotSource(**values)  # type: ignore[arg-type]


def launch(**overrides: float) -> BallLaunch:
    values: dict[str, float] = {
        "speed_mph": 148.2,
        "launch_angle_degrees": 13.4,
        "launch_direction_degrees": 1.5,
    }
    values.update(overrides)
    return BallLaunch(**values)


def test_a_ball_launch_holds_what_every_device_measures() -> None:
    ball = launch(total_spin_rpm=2750.0, carry_yards=268.4)

    assert ball.speed_mph == 148.2
    assert ball.total_spin_rpm == 2750.0
    assert ball.carry_yards == 268.4


@pytest.mark.parametrize(
    "field", ["speed_mph", "launch_angle_degrees", "launch_direction_degrees"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_ball_launch_rejects_numbers_that_are_not_numbers(
    field: str, value: float
) -> None:
    # These cannot be sent as JSON and they poison every average built on them.
    with pytest.raises(ValueError, match="finite"):
        launch(**{field: value})


def test_a_ball_launch_rejects_a_negative_speed() -> None:
    with pytest.raises(ValueError, match="negative"):
        launch(speed_mph=-1.0)


def test_optional_measurements_may_be_absent() -> None:
    ball = launch()

    assert ball.total_spin_rpm is None
    assert ball.carry_yards is None


def test_a_club_delivery_rejects_numbers_that_are_not_numbers() -> None:
    with pytest.raises(ValueError, match="finite"):
        ClubDelivery(speed_mph=float("nan"))


def test_face_to_path_is_the_difference_between_face_and_path() -> None:
    delivery = ClubDelivery(face_to_target_degrees=1.0, path_degrees=-3.0)

    assert delivery.face_to_path_degrees == pytest.approx(4.0)


def test_face_to_path_needs_both_inputs() -> None:
    assert ClubDelivery(face_to_target_degrees=1.0).face_to_path_degrees is None
    assert ClubDelivery(path_degrees=1.0).face_to_path_degrees is None


def test_an_empty_club_delivery_knows_it_is_empty() -> None:
    # A device sending a block of nothing but nulls has sent no club data.
    assert ClubDelivery().is_empty
    assert not ClubDelivery(speed_mph=90.0).is_empty


def test_a_shot_needs_at_least_something_measured() -> None:
    with pytest.raises(ValueError, match="ball data, club data, or both"):
        Shot(shot_id=uuid4(), source=source())


def test_a_shot_reports_which_data_it_has() -> None:
    shot = Shot(shot_id=uuid4(), source=source(), ball=launch())

    assert shot.has_ball_data
    assert not shot.has_club_data


def test_a_shot_is_immutable() -> None:
    # Shots are facts. A published shot cannot be edited after the event.
    shot = Shot(shot_id=uuid4(), source=source(), ball=launch())

    with pytest.raises(AttributeError):
        shot.ball = None  # type: ignore[misc]


def test_a_shot_starts_with_no_club_because_no_device_reports_one() -> None:
    # Correlating the club in play with a shot is domain work, not ingestion.
    shot = Shot(shot_id=uuid4(), source=source(), ball=launch())

    assert shot.club is None
    assert shot.handedness is Handedness.UNKNOWN


def test_a_club_keeps_the_vendor_code_beside_the_category() -> None:
    club = Club(kind=ClubKind.IRON, vendor_code="I7")

    assert club.kind is ClubKind.IRON
    assert club.vendor_code == "I7"


def test_detection_distinguishes_absent_from_unknown() -> None:
    # A device that never reports detection must not read as reporting no ball.
    assert Detection.ABSENT is not Detection.UNKNOWN
    assert len(Detection) == 3


def test_provenance_records_when_the_shot_was_observed() -> None:
    observed = ShotSource(
        kind="gspro_connect",
        device_session_id=uuid4(),
        observed_at=datetime.now(UTC),
        observed_monotonic=99.0,
        device_id="ProTee VX",
        device_sequence=12,
    )

    assert observed.device_id == "ProTee VX"
    assert observed.device_sequence == 12
    assert observed.observed_at.tzinfo is not None
