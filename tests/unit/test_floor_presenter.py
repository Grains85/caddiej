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
from caddiej.floor.geometry import PhysicalPoint, PhysicalRect, rectangular_calibration
from caddiej.floor.layout import PlacementMode, layout_from_tees, placement_mode_for
from caddiej.floor.presenter import FloorPresenter, marker_variant_for
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorPoint, HittingStrip, MarkerVariant

TEE = PhysicalPoint(x_m=0.5, y_m=0.72)
DRIVER_TEE = PhysicalPoint(x_m=0.5, y_m=0.55)
SURFACE_TEE = FloorPoint(x=0.5, y=0.72)
SURFACE_DRIVER_TEE = FloorPoint(x=0.5, y=0.55)
# The putter's default offset: 0.30 m down-range of the strip centre, which on
# the default 0.45 m strip is off the front of the mat. That is deliberate.
SURFACE_PUTTER = FloorPoint(x=0.5, y=0.42)


def presenter(floor: FloorService) -> FloorPresenter:
    return FloorPresenter(
        floor, layout_from_tees(tee=TEE, driver_tee=DRIVER_TEE), club_path_length=0.4
    )


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


def test_ball_placement_is_one_mapping_not_string_compares() -> None:
    assert placement_mode_for(Club(kind=ClubKind.DRIVER)) is PlacementMode.DRIVER
    assert placement_mode_for(Club(kind=ClubKind.PUTTER)) is PlacementMode.PUTTER
    assert placement_mode_for(Club(kind=ClubKind.IRON)) is PlacementMode.IRON
    assert placement_mode_for(Club(kind=ClubKind.HYBRID)) is PlacementMode.IRON
    assert placement_mode_for(None) is PlacementMode.IRON


def test_club_tees_are_mapped_through_floor_calibration() -> None:
    floor = FloorService()
    subject = FloorPresenter(
        floor,
        layout_from_tees(
            tee=PhysicalPoint(x_m=0.5, y_m=0.72),
            driver_tee=PhysicalPoint(x_m=0.4, y_m=0.30),
        ),
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
    # Where the putter's ball goes, which is not where an iron's does.
    assert floor.state.marker.position == SURFACE_PUTTER
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
    assert floor.state.round_result.shot_distance_display == "249 m"
    # currentRound.dat carried no DistanceToPin for this shot, so the panel gets
    # no remaining figure to draw rather than a dash under a "TO PIN" label.
    assert floor.state.round_result.distance_to_pin_raw is None
    assert floor.state.round_result.remaining_distance_display is None
    assert floor.state.ball_flight is not None
    assert len(floor.state.ball_flight) == 2


def test_the_hitting_strip_is_physical_and_mapped_through_calibration() -> None:
    floor = FloorService()
    subject = FloorPresenter(
        floor,
        layout_from_tees(
            PhysicalRect(x_m=2.0, y_m=1.5, width_m=0.40, depth_m=0.60),
            tee=PhysicalPoint(x_m=2.0, y_m=1.5),
        ),
        club_path_length=0.4,
        calibration=rectangular_calibration(4.0, 3.0),
    )

    subject.light_hitting_area()

    strip = floor.state.hitting_strip
    assert strip is not None
    assert strip.visible is True
    back_left, back_right, front_right, front_left = strip.corners
    assert back_left.x == pytest.approx(0.45)
    assert back_left.y == pytest.approx(0.4)
    assert back_right.x == pytest.approx(0.55)
    assert front_right.y == pytest.approx(0.6)
    assert front_left.x == pytest.approx(0.45)


def test_width_and_depth_size_the_strip_independently_of_its_centre() -> None:
    """Four separate metre dials. A wider pad must not also become a deeper one,
    and neither may move where it sits."""

    floor = FloorService()
    subject = FloorPresenter(
        floor,
        layout_from_tees(
            PhysicalRect(x_m=1.0, y_m=2.4, width_m=1.20, depth_m=0.30),
            tee=PhysicalPoint(x_m=2.0, y_m=1.5),
        ),
        calibration=rectangular_calibration(4.0, 3.0),
    )

    subject.light_hitting_area()

    strip = floor.state.hitting_strip
    assert strip is not None
    back_left, back_right, front_right, _front_left = strip.corners
    # Centre 1.0 m across a 4 m bay, 1.2 m wide: 0.4 m to 1.6 m, so 0.1 to 0.4.
    assert back_left.x == pytest.approx(0.1)
    assert back_right.x == pytest.approx(0.4)
    # Centre 2.4 m up a 3 m bay, 0.3 m deep: 2.25 m to 2.55 m, so 0.75 to 0.85.
    assert back_left.y == pytest.approx(0.75)
    assert front_right.y == pytest.approx(0.85)


def test_the_strip_centre_does_not_follow_the_tee_when_it_is_given() -> None:
    """The ball sits where the player wants it on the pad; the pad is where the
    turf is. Two measurements, and the strip's own wins."""

    floor = FloorService()
    subject = FloorPresenter(
        floor,
        layout_from_tees(
            PhysicalRect(x_m=0.48, y_m=0.85, width_m=0.30, depth_m=0.45),
            tee=PhysicalPoint(x_m=0.20, y_m=0.20),
        ),
    )

    subject.light_hitting_area()
    subject.show_address(None)

    strip = floor.state.hitting_strip
    assert strip is not None
    back_left, back_right, front_right, _front_left = strip.corners
    assert back_left.x == pytest.approx(0.33)
    assert back_right.x == pytest.approx(0.63)
    assert back_left.y == pytest.approx(0.625)
    assert front_right.y == pytest.approx(1.075)
    # The marker stays at the tee. Moving the strip did not move the ball.
    assert floor.state.marker.position.x == pytest.approx(0.20)
    assert floor.state.marker.position.y == pytest.approx(0.20)


class TestLightingTheTurf:
    """FLOOR_HITTING_STRIP_FILL, which is the difference between an outline and a
    work light over the physical mat. The geometry is identical either way."""

    def strip(self, *, filled: bool) -> HittingStrip:
        floor = FloorService()
        subject = FloorPresenter(
            floor,
            layout_from_tees(
                PhysicalRect(x_m=0.48, y_m=0.85, width_m=0.30, depth_m=0.45),
                tee=PhysicalPoint(x_m=0.5, y_m=0.72),
                strip_filled=filled,
            ),
        )
        subject.light_hitting_area()
        strip = floor.state.hitting_strip
        assert strip is not None
        return strip

    def test_off_by_default_is_an_outline(self) -> None:
        assert self.strip(filled=False).filled is False

    def test_on_asks_for_the_whole_polygon_to_be_lit(self) -> None:
        assert self.strip(filled=True).filled is True

    def test_lighting_it_changes_nothing_about_where_it_is(self) -> None:
        assert self.strip(filled=True).corners == self.strip(filled=False).corners

    def test_a_hidden_strip_still_knows_it_was_a_lit_one(self) -> None:
        """Visibility is a flag over kept geometry, so the overlay fades the same
        pad out rather than a differently styled one."""

        floor = FloorService()
        subject = FloorPresenter(
            floor,
            layout_from_tees(tee=PhysicalPoint(x_m=0.5, y_m=0.72), strip_filled=True),
        )
        subject.light_hitting_area()
        subject.darken_hitting_area()

        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is False
        assert floor.state.hitting_strip.filled is True


class TestTheTurfLightIsNotPartOfTheAddress:
    """The real-hardware bug, as an invariant.

    The strip used to be lit by `show_address` and put out by `hide_address`,
    which made the light over a physical mat a property of whether a launch
    monitor had seen a ball. It went dark under the player's feet every time one
    was detected. Lighting the bay is now its own call, and nothing on the
    address path may touch it in either direction.
    """

    def floor_with_a_lit_strip(self) -> tuple[FloorService, FloorPresenter]:
        floor = FloorService()
        subject = FloorPresenter(
            floor,
            layout_from_tees(
                PhysicalRect(x_m=0.48, y_m=0.85, width_m=0.30, depth_m=0.45),
                tee=PhysicalPoint(x_m=0.5, y_m=0.72),
                strip_filled=True,
            ),
        )
        subject.light_hitting_area()
        return floor, subject

    def test_hiding_the_marker_leaves_the_turf_lit(self) -> None:
        floor, subject = self.floor_with_a_lit_strip()

        subject.hide_address()

        assert floor.state.marker.visible is False
        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is True
        assert floor.state.hitting_strip.filled is True

    def test_showing_the_pre_shot_numbers_leaves_the_turf_lit(self) -> None:
        floor, subject = self.floor_with_a_lit_strip()

        subject.show_address_hud(None, 147.2)

        assert floor.state.address is not None
        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is True

    def test_a_round_result_leaves_the_turf_lit(self) -> None:
        floor, subject = self.floor_with_a_lit_strip()

        subject.show_round_result(
            ObservedRoundShot(shot_id="A", total_distance_raw=35.46979)
        )

        assert floor.state.round_result is not None
        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is True

    def test_only_darkening_it_puts_it_out_and_the_geometry_survives(self) -> None:
        floor, subject = self.floor_with_a_lit_strip()
        corners = floor.state.hitting_strip.corners if floor.state.hitting_strip else ()

        subject.darken_hitting_area()

        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is False
        assert floor.state.hitting_strip.corners == corners


def test_hitting_strip_source_geometry_is_metres_not_css_pixels() -> None:
    rect = PhysicalRect(x_m=0.5, y_m=0.72, width_m=0.30, depth_m=0.45)
    assert rect.width_m == 0.30
    assert rect.depth_m == 0.45
    assert "px" not in str(rect)
