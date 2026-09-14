"""Turning a shot into something on the floor."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.domain.observation import BallPathPoint, ObservedRoundShot, WorldPoint
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Shot,
    ShotSource,
)
from caddiej.floor.geometry import PhysicalPoint, rectangular_calibration
from caddiej.floor.presenter import FloorPresenter, marker_variant_for
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorPoint, MarkerVariant
from caddiej.floor.tees import TeePositions

TEE = PhysicalPoint(x_m=0.5, y_m=0.72)
DRIVER_TEE = PhysicalPoint(x_m=0.5, y_m=0.55)
SURFACE_TEE = FloorPoint(x=0.5, y=0.72)
SURFACE_DRIVER_TEE = FloorPoint(x=0.5, y=0.55)


def presenter(floor: FloorService) -> FloorPresenter:
    return FloorPresenter(floor, tee=TEE, driver_tee=DRIVER_TEE, club_path_length=0.4)


def shot(
    ball: BallLaunch | None = None,
    club_delivery: ClubDelivery | None = None,
) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=ball,
        club_delivery=club_delivery,
    )


def launch(carry: float | None = 268.4) -> BallLaunch:
    return BallLaunch(
        speed_mph=148.2,
        launch_angle_degrees=12.6,
        launch_direction_degrees=1.5,
        carry_yards=carry,
    )


# --- which marker a club calls for ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ClubKind.DRIVER, MarkerVariant.DRIVER),
        (ClubKind.PUTTER, MarkerVariant.PUTTER),
        (ClubKind.IRON, MarkerVariant.NORMAL),
        (ClubKind.WEDGE, MarkerVariant.NORMAL),
        (ClubKind.WOOD, MarkerVariant.NORMAL),
        (ClubKind.HYBRID, MarkerVariant.NORMAL),
        (ClubKind.UNKNOWN, MarkerVariant.NORMAL),
    ],
)
def test_the_marker_follows_the_club(kind: ClubKind, expected: MarkerVariant) -> None:
    assert marker_variant_for(Club(kind=kind)) is expected


def test_an_unknown_club_still_gets_somewhere_to_put_the_ball() -> None:
    # Before the simulator has said anything, the player still needs a marker.
    assert marker_variant_for(None) is MarkerVariant.NORMAL


def test_showing_the_address_places_the_marker_at_the_tee() -> None:
    floor = FloorService()

    presenter(floor).show_address(Club(kind=ClubKind.IRON))

    assert floor.state.marker.position == SURFACE_TEE
    assert floor.state.marker.variant is MarkerVariant.NORMAL
    assert floor.state.marker.visible is True


def test_a_driver_uses_the_driver_physical_tee() -> None:
    floor = FloorService()

    presenter(floor).show_address(Club(kind=ClubKind.DRIVER))

    assert floor.state.marker.position == SURFACE_DRIVER_TEE
    assert floor.state.marker.variant is MarkerVariant.DRIVER


def test_an_unknown_club_uses_the_default_physical_tee() -> None:
    floor = FloorService()

    presenter(floor).show_address(None)

    assert floor.state.marker.position == SURFACE_TEE
    assert floor.state.marker.variant is MarkerVariant.NORMAL


def test_tee_selection_is_one_mapping_not_string_compares() -> None:
    tees = TeePositions(default=TEE, by_kind={ClubKind.DRIVER: DRIVER_TEE})

    assert tees.for_club(Club(kind=ClubKind.DRIVER)) == DRIVER_TEE
    assert tees.for_club(Club(kind=ClubKind.IRON)) == TEE
    assert tees.for_club(Club(kind=ClubKind.PUTTER)) == TEE
    assert tees.for_club(None) == TEE


def test_club_tees_are_mapped_through_floor_calibration() -> None:
    floor = FloorService()
    subject = FloorPresenter(
        floor,
        tee=PhysicalPoint(x_m=0.5, y_m=0.72),
        driver_tee=PhysicalPoint(x_m=0.4, y_m=0.30),
        club_path_length=0.4,
        calibration=rectangular_calibration(4.0, 3.0),
    )

    subject.show_address(Club(kind=ClubKind.DRIVER))
    assert floor.state.marker.position.x == pytest.approx(0.1)
    assert floor.state.marker.position.y == pytest.approx(0.1)

    subject.show_address(Club(kind=ClubKind.IRON))
    assert floor.state.marker.position.x == pytest.approx(0.125)
    assert floor.state.marker.position.y == pytest.approx(0.24)


def test_showing_the_address_stands_the_startup_banner_down() -> None:
    # It is drawn across the whole surface, so the marker would be behind it.
    floor = FloorService()
    floor.set_online("CADDIEJ FLOOR ONLINE")

    presenter(floor).show_address(None)

    assert floor.state.status_text == ""


def test_a_shot_stands_the_startup_banner_down() -> None:
    # A device that reports a shot without ever reporting a ball or a club would
    # otherwise put its numbers behind the banner.
    floor = FloorService()
    floor.set_online("CADDIEJ FLOOR ONLINE")

    presenter(floor).show_shot(shot(ball=launch()))

    assert floor.state.status_text == ""


def test_hiding_the_address_keeps_where_the_marker_was() -> None:
    floor = FloorService()
    subject = presenter(floor)
    subject.show_address(Club(kind=ClubKind.PUTTER))

    subject.hide_address()

    assert floor.state.marker.visible is False
    assert floor.state.marker.position == SURFACE_TEE
    assert floor.state.marker.variant is MarkerVariant.PUTTER


# --- what a shot looks like -----------------------------------------------


def test_a_shot_draws_the_club_path_from_the_tee() -> None:
    floor = FloorService()

    presenter(floor).show_shot(
        shot(ball=launch(), club_delivery=ClubDelivery(path_degrees=-2.4))
    )

    path = floor.state.club_path
    assert path is not None
    assert path.start == SURFACE_TEE
    assert path.angle_degrees == -2.4
    assert path.length == 0.4


def test_a_shot_shows_the_launch_numbers() -> None:
    floor = FloorService()

    presenter(floor).show_shot(shot(ball=launch()))

    metrics = floor.state.metrics
    assert metrics is not None
    assert metrics.ball_speed_mph == 148.2
    assert metrics.launch_angle_degrees == 12.6
    assert metrics.carry_yards == 268.4


def test_a_shot_from_a_device_that_measures_no_carry_omits_it() -> None:
    # Most launch monitors do not measure carry; the simulator computes it. A
    # zero would be a number a player might believe.
    floor = FloorService()

    presenter(floor).show_shot(shot(ball=launch(carry=None)))

    assert floor.state.metrics is not None
    assert floor.state.metrics.carry_yards is None


def test_a_shot_with_no_club_data_draws_no_club_path() -> None:
    # Drawing the ball's launch direction and calling it the club path would be
    # inventing a measurement the device never took.
    floor = FloorService()

    presenter(floor).show_shot(shot(ball=launch()))

    assert floor.state.club_path is None


def test_a_club_block_without_a_path_draws_no_club_path() -> None:
    floor = FloorService()

    presenter(floor).show_shot(
        shot(ball=launch(), club_delivery=ClubDelivery(speed_mph=104.5))
    )

    assert floor.state.club_path is None


def test_a_club_only_shot_shows_a_path_but_no_numbers() -> None:
    floor = FloorService()

    presenter(floor).show_shot(shot(club_delivery=ClubDelivery(path_degrees=3.0)))

    assert floor.state.club_path is not None
    assert floor.state.metrics is None


def test_clearing_a_shot_removes_both_the_path_and_the_numbers() -> None:
    floor = FloorService()
    subject = presenter(floor)
    subject.show_shot(shot(ball=launch(), club_delivery=ClubDelivery(path_degrees=1.0)))

    subject.clear_shot()

    assert floor.state.club_path is None
    assert floor.state.metrics is None
    assert floor.state.round_result is None


def test_a_second_shot_replaces_the_first_rather_than_adding_to_it() -> None:
    floor = FloorService()
    subject = presenter(floor)
    subject.show_shot(
        shot(ball=launch(), club_delivery=ClubDelivery(path_degrees=10.0))
    )

    subject.show_shot(
        shot(ball=launch(), club_delivery=ClubDelivery(path_degrees=-10.0))
    )

    assert floor.state.club_path is not None
    assert floor.state.club_path.angle_degrees == -10.0


def test_a_round_result_shows_raw_numbers_and_a_flight_trace() -> None:
    floor = FloorService()
    presenter(floor).show_round_result(
        ObservedRoundShot(
            shot_id="D",
            player_name="Johan",
            hole=3,
            hole_shot=2,
            ball_speed_raw=32.84,
            total_distance_raw=248.5,
            starting_surface_raw=1,
            ending_surface_raw=4,
            ball_start=WorldPoint(x=10.0, y=1.0, z=20.0),
            ball_path=(
                BallPathPoint(time=0.0, x=10.0, y=1.0, z=20.0),
                BallPathPoint(time=0.5, x=14.0, y=3.0, z=28.0),
            ),
        )
    )

    assert floor.state.metrics is None
    assert floor.state.club_path is None
    assert floor.state.round_result is not None
    assert floor.state.round_result.ball_speed_raw == 32.84
    assert floor.state.round_result.total_distance_raw == 248.5
    assert floor.state.round_result.ball_speed_display == "118 km/h"
    assert floor.state.round_result.total_distance_display == "249 m"
    assert floor.state.ball_flight is not None
    assert len(floor.state.ball_flight) == 2
