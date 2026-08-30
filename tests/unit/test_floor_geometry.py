"""Physical floor geometry: angles live in metres, not in the unit square.

The overlay is a 0..1 surface. A hitting bay is not a square. These tests exist
so a 10° club path stays 10° on a 4 m × 3 m floor, and so changing the
projector or the OBS canvas cannot talk that number into a different one.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.domain.shot import BallLaunch, ClubDelivery, Shot, ShotSource
from caddiej.floor import (
    DEFAULT_CALIBRATION,
    UNIT_SURFACE_CORNERS,
    ClubPath,
    FloorPoint,
    FloorPresenter,
    FloorService,
    FloorState,
    HomographyCalibration,
    PhysicalClubPath,
    PhysicalFloor,
    PhysicalPoint,
    RectangularCalibration,
    display_to_surface,
    path_angle_degrees,
    physical_endpoint,
    rectangular_calibration,
    surface_to_display,
)
from caddiej.floor.geometry import DisplayPoint


def _shot(path_degrees: float) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=148.2,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            carry_yards=268.4,
        ),
        club_delivery=ClubDelivery(path_degrees=path_degrees),
    )


FOUR_BY_THREE = PhysicalFloor(width_m=4.0, depth_m=3.0)
FOUR_BY_THREE_CAL = RectangularCalibration(FOUR_BY_THREE)
TEE = FloorPoint(x=0.5, y=0.8)


def old_normalized_end(
    start: FloorPoint, angle_degrees: float, length: float
) -> FloorPoint:
    """The previous formula: trigonometry in the unit square.

    Kept as a reference so the default 1 m × 1 m calibration can be shown to
    reproduce today's endpoints, and so a 4 m × 3 m floor can be shown not to.
    """

    radians = math.radians(angle_degrees)
    return FloorPoint(
        x=start.x + length * math.sin(radians),
        y=start.y - length * math.cos(radians),
    )


# --- A. 0° is exactly down-range ------------------------------------------


def test_a_zero_degree_path_points_exactly_down_range() -> None:
    start = PhysicalPoint(x_m=2.0, y_m=2.4)
    end = physical_endpoint(start, angle_degrees=0.0, length_m=1.0)

    assert end.x_m == pytest.approx(2.0)
    assert end.y_m == pytest.approx(1.4)
    assert path_angle_degrees(start, end) == pytest.approx(0.0)


def test_a_zero_degree_surface_path_does_not_drift_sideways_on_a_4_by_3_floor() -> None:
    path = ClubPath(
        start=TEE, angle_degrees=0.0, length=1.0, calibration=FOUR_BY_THREE_CAL
    )

    assert path.end.x == pytest.approx(TEE.x)
    assert path.end.y < TEE.y


# --- B. +10° and −10° are physically symmetric ----------------------------


def test_plus_and_minus_ten_degrees_are_physically_symmetric() -> None:
    start = PhysicalPoint(x_m=2.0, y_m=2.4)
    right = PhysicalClubPath(start=start, angle_degrees=10.0, length_m=1.0)
    left = PhysicalClubPath(start=start, angle_degrees=-10.0, length_m=1.0)

    assert right.end.y_m == pytest.approx(left.end.y_m)
    assert right.end.x_m - start.x_m == pytest.approx(start.x_m - left.end.x_m)
    assert path_angle_degrees(start, right.end) == pytest.approx(10.0)
    assert path_angle_degrees(start, left.end) == pytest.approx(-10.0)


def test_plus_and_minus_ten_remain_symmetric_after_mapping_onto_a_4_by_3_surface() -> (
    None
):
    right = ClubPath(
        start=TEE, angle_degrees=10.0, length=1.0, calibration=FOUR_BY_THREE_CAL
    )
    left = ClubPath(
        start=TEE, angle_degrees=-10.0, length=1.0, calibration=FOUR_BY_THREE_CAL
    )

    assert right.end.y == pytest.approx(left.end.y)
    assert right.end.x - TEE.x == pytest.approx(TEE.x - left.end.x)


# --- C. Render resolution cannot change a physical angle ------------------


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (800, 600), (1024, 768)])
def test_the_same_physical_angle_survives_any_render_resolution(
    size: tuple[int, int],
) -> None:
    width_px, height_px = size
    path = ClubPath(
        start=TEE, angle_degrees=10.0, length=1.2, calibration=FOUR_BY_THREE_CAL
    )

    start_px = surface_to_display(path.start, width_px, height_px)
    end_px = surface_to_display(path.end, width_px, height_px)
    start_back = FOUR_BY_THREE_CAL.to_physical(
        display_to_surface(start_px, width_px, height_px)
    )
    end_back = FOUR_BY_THREE_CAL.to_physical(
        display_to_surface(end_px, width_px, height_px)
    )

    assert path_angle_degrees(start_back, end_back) == pytest.approx(10.0)


def test_two_resolutions_recover_the_same_physical_endpoints() -> None:
    path = ClubPath(
        start=TEE, angle_degrees=-7.5, length=0.9, calibration=FOUR_BY_THREE_CAL
    )
    recovered = []
    for width_px, height_px in ((1920, 1080), (800, 600)):
        start = FOUR_BY_THREE_CAL.to_physical(
            display_to_surface(
                surface_to_display(path.start, width_px, height_px),
                width_px,
                height_px,
            )
        )
        end = FOUR_BY_THREE_CAL.to_physical(
            display_to_surface(
                surface_to_display(path.end, width_px, height_px),
                width_px,
                height_px,
            )
        )
        recovered.append((start, end))

    (start_a, end_a), (start_b, end_b) = recovered
    assert start_a.x_m == pytest.approx(start_b.x_m)
    assert start_a.y_m == pytest.approx(start_b.y_m)
    assert end_a.x_m == pytest.approx(end_b.x_m)
    assert end_a.y_m == pytest.approx(end_b.y_m)


# --- D. 45° on 4×3 is not equal normalized dx/dy --------------------------


def test_a_45_degree_physical_line_is_not_45_degrees_in_normalized_space() -> None:
    # The bug: treating the unit square as if it were isotropic. Equal surface
    # dx/dy on a 4 m × 3 m floor is atan(4/3) ≈ 53.13°, not 45°.
    path = ClubPath(
        start=TEE, angle_degrees=45.0, length=1.0, calibration=FOUR_BY_THREE_CAL
    )
    dx = path.end.x - path.start.x
    dy = path.end.y - path.start.y

    assert abs(dx) != pytest.approx(abs(dy))
    physical = PhysicalClubPath(
        start=FOUR_BY_THREE_CAL.to_physical(path.start),
        angle_degrees=45.0,
        length_m=1.0,
    )
    assert path.end.x == pytest.approx(physical.end.x_m / 4.0)
    assert path.end.y == pytest.approx(physical.end.y_m / 3.0)
    assert path_angle_degrees(physical.start, physical.end) == pytest.approx(45.0)


def test_equal_normalized_steps_are_not_a_physical_45_on_a_4_by_3_floor() -> None:
    start = PhysicalPoint(x_m=2.0, y_m=2.4)
    # A line that moves 0.1 of the surface in x and 0.1 in y.
    fake_end = FOUR_BY_THREE_CAL.to_physical(FloorPoint(x=0.5 + 0.1, y=0.8 - 0.1))

    assert path_angle_degrees(start, fake_end) == pytest.approx(
        math.degrees(math.atan2(0.4, 0.3))
    )
    assert path_angle_degrees(start, fake_end) == pytest.approx(53.130102354, abs=1e-6)


# --- E. Known points through physical → surface → display -----------------


def test_the_physical_corners_map_onto_the_unit_square() -> None:
    mapping = FOUR_BY_THREE_CAL
    back_left, back_right, front_right, front_left = FOUR_BY_THREE.corners

    assert mapping.to_surface(back_left) == FloorPoint(x=0.0, y=0.0)
    assert mapping.to_surface(back_right) == FloorPoint(x=1.0, y=0.0)
    assert mapping.to_surface(front_right) == FloorPoint(x=1.0, y=1.0)
    assert mapping.to_surface(front_left) == FloorPoint(x=0.0, y=1.0)
    assert mapping.to_physical(FloorPoint(x=0.5, y=0.5)) == PhysicalPoint(
        x_m=2.0, y_m=1.5
    )


def test_surface_to_display_is_a_pixel_stretch() -> None:
    point = FloorPoint(x=0.25, y=0.8)
    pixels = surface_to_display(point, width_px=1920, height_px=1080)

    assert pixels == DisplayPoint(x_px=480.0, y_px=864.0)


def test_a_worked_4_by_3_ten_degree_path_matches_the_hand_calculation() -> None:
    # Tee at surface (0.5, 0.8) is physical (2.0 m, 2.4 m). A 10° / 1 m ray:
    #   dx = sin(10°) ≈ 0.173648 m
    #   dy = −cos(10°) ≈ −0.984808 m
    #   end = (2.173648, 1.415192) → surface (0.543412, 0.471731)
    start = FOUR_BY_THREE_CAL.to_physical(TEE)
    assert start.x_m == pytest.approx(2.0)
    assert start.y_m == pytest.approx(2.4)

    path = PhysicalClubPath(start=start, angle_degrees=10.0, length_m=1.0)
    assert path.end.x_m == pytest.approx(2.0 + math.sin(math.radians(10.0)))
    assert path.end.y_m == pytest.approx(2.4 - math.cos(math.radians(10.0)))

    surface_end = FOUR_BY_THREE_CAL.to_surface(path.end)
    assert surface_end.x == pytest.approx(path.end.x_m / 4.0)
    assert surface_end.y == pytest.approx(path.end.y_m / 3.0)

    drawn = ClubPath(
        start=TEE, angle_degrees=10.0, length=1.0, calibration=FOUR_BY_THREE_CAL
    )
    assert drawn.end.x == pytest.approx(surface_end.x)
    assert drawn.end.y == pytest.approx(surface_end.y)

    pixels = surface_to_display(drawn.end, width_px=1920, height_px=1080)
    assert pixels.x_px == pytest.approx(surface_end.x * 1920)
    assert pixels.y_px == pytest.approx(surface_end.y * 1080)


def test_a_rectangular_map_agrees_with_a_homography_of_the_same_rectangle() -> None:
    homography = HomographyCalibration.from_correspondences(
        FOUR_BY_THREE.corners, UNIT_SURFACE_CORNERS
    )
    sample = PhysicalPoint(x_m=1.2, y_m=2.1)

    assert homography.to_surface(sample).x == pytest.approx(
        FOUR_BY_THREE_CAL.to_surface(sample).x
    )
    assert homography.to_surface(sample).y == pytest.approx(
        FOUR_BY_THREE_CAL.to_surface(sample).y
    )
    recovered = homography.to_physical(FOUR_BY_THREE_CAL.to_surface(sample))
    assert recovered.x_m == pytest.approx(sample.x_m)
    assert recovered.y_m == pytest.approx(sample.y_m)


# --- F. Degenerate calibration is refused ---------------------------------


@pytest.mark.parametrize(
    ("width_m", "depth_m"),
    [(0.0, 3.0), (-4.0, 3.0), (4.0, 0.0), (4.0, -1.0)],
)
def test_a_non_positive_floor_is_rejected(width_m: float, depth_m: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        PhysicalFloor(width_m=width_m, depth_m=depth_m)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_floor_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        PhysicalFloor(width_m=bad, depth_m=3.0)


def test_a_homography_with_the_wrong_number_of_points_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly four"):
        HomographyCalibration.from_correspondences(
            FOUR_BY_THREE.corners[:3], UNIT_SURFACE_CORNERS[:3]
        )


def test_coincident_calibration_points_are_rejected() -> None:
    physical = (
        PhysicalPoint(x_m=0.0, y_m=0.0),
        PhysicalPoint(x_m=4.0, y_m=0.0),
        PhysicalPoint(x_m=4.0, y_m=3.0),
        PhysicalPoint(x_m=0.0, y_m=0.0),
    )
    with pytest.raises(ValueError, match="degenerate"):
        HomographyCalibration.from_correspondences(physical, UNIT_SURFACE_CORNERS)


def test_collinear_calibration_points_are_rejected() -> None:
    physical = (
        PhysicalPoint(x_m=0.0, y_m=0.0),
        PhysicalPoint(x_m=1.0, y_m=0.0),
        PhysicalPoint(x_m=2.0, y_m=0.0),
        PhysicalPoint(x_m=3.0, y_m=0.0),
    )
    with pytest.raises(ValueError, match="collinear"):
        HomographyCalibration.from_correspondences(physical, UNIT_SURFACE_CORNERS)


def test_a_collapsed_surface_quad_is_rejected() -> None:
    flat = (
        FloorPoint(x=0.0, y=0.0),
        FloorPoint(x=1.0, y=0.0),
        FloorPoint(x=1.0, y=0.0),
        FloorPoint(x=0.0, y=0.0),
    )
    with pytest.raises(ValueError, match="degenerate"):
        HomographyCalibration.from_correspondences(FOUR_BY_THREE.corners, flat)


# --- G. Default calibration does not move today's markers or paths --------


def test_the_default_calibration_is_the_identity_on_the_unit_floor() -> None:
    point = FloorPoint(x=0.5, y=0.72)
    physical = DEFAULT_CALIBRATION.to_physical(point)
    assert physical == PhysicalPoint(x_m=0.5, y_m=0.72)
    assert DEFAULT_CALIBRATION.to_surface(physical) == point


def test_default_club_path_endpoints_match_the_old_normalized_formula() -> None:
    for angle, length in ((0.0, 0.3), (90.0, 0.2), (-30.0, 0.5), (37.0, 0.25)):
        start = FloorPoint(x=0.5, y=0.8)
        path = ClubPath(start=start, angle_degrees=angle, length=length)
        expected = old_normalized_end(start, angle, length)
        assert path.end.x == pytest.approx(expected.x)
        assert path.end.y == pytest.approx(expected.y)


def test_the_configured_tee_still_lands_on_the_same_surface_point() -> None:
    # Marker placement is surface coordinates, set by eye. Changing the
    # physical size of the bay must not slide the marker; it changes what a
    # metre means, not where the operator put the ball.
    floor = FloorService()
    presenter = FloorPresenter(
        floor,
        tee=FloorPoint(x=0.5, y=0.72),
        club_path_length=0.35,
        calibration=FOUR_BY_THREE_CAL,
    )

    presenter.show_address(None)

    assert floor.state.marker.position == FloorPoint(x=0.5, y=0.72)


def test_the_presenter_draws_a_physical_path_from_the_configured_tee() -> None:
    floor = FloorService()
    presenter = FloorPresenter(
        floor,
        tee=FloorPoint(x=0.5, y=0.72),
        club_path_length=1.0,
        calibration=FOUR_BY_THREE_CAL,
    )

    presenter.show_shot(_shot(45.0))

    path = floor.state.club_path
    assert path is not None
    assert path.start == FloorPoint(x=0.5, y=0.72)
    assert path.angle_degrees == 45.0
    assert abs(path.end.x - path.start.x) != pytest.approx(
        abs(path.end.y - path.start.y)
    )
    recovered = path_angle_degrees(
        FOUR_BY_THREE_CAL.to_physical(path.start),
        FOUR_BY_THREE_CAL.to_physical(path.end),
    )
    assert recovered == pytest.approx(45.0)


# --- H. The wire carries only finite numbers ------------------------------


def test_a_mapped_path_serialises_to_finite_json() -> None:
    state = FloorState().evolve(
        club_path=ClubPath(
            start=TEE,
            angle_degrees=12.5,
            length=1.1,
            calibration=FOUR_BY_THREE_CAL,
        )
    )

    raw = snapshot_message(state).model_dump_json()

    assert "NaN" not in raw
    assert "Infinity" not in raw
    payload = json.loads(raw)
    end = payload["state"]["club_path"]["end"]
    assert math.isfinite(end["x"])
    assert math.isfinite(end["y"])


def test_a_homography_mapped_path_serialises_to_finite_json() -> None:
    # A mild keystone: the back edge is narrower than the front, still a
    # convex quad. The wizard that would measure this does not exist yet;
    # the mapping that would consume it does.
    keystone = HomographyCalibration.from_correspondences(
        FOUR_BY_THREE.corners,
        (
            FloorPoint(x=0.1, y=0.0),
            FloorPoint(x=0.9, y=0.0),
            FloorPoint(x=1.0, y=1.0),
            FloorPoint(x=0.0, y=1.0),
        ),
    )
    state = FloorState().evolve(
        club_path=ClubPath(
            start=TEE, angle_degrees=-8.0, length=0.8, calibration=keystone
        )
    )

    raw = snapshot_message(state).model_dump_json()
    parsed = json.loads(raw)["state"]["club_path"]
    for key in ("x", "y"):
        assert math.isfinite(parsed["start"][key])
        assert math.isfinite(parsed["end"][key])


# --- construction guards ---------------------------------------------------


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_physical_points_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        PhysicalPoint(x_m=bad, y_m=1.0)


def test_a_negative_physical_path_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        PhysicalClubPath(
            start=PhysicalPoint(x_m=1.0, y_m=1.0),
            angle_degrees=0.0,
            length_m=-0.1,
        )


def test_rectangular_calibration_factory_matches_the_explicit_type() -> None:
    mapping = rectangular_calibration(4.0, 3.0)
    assert mapping.floor == FOUR_BY_THREE
    assert mapping.to_surface(PhysicalPoint(x_m=2.0, y_m=1.5)) == FloorPoint(
        x=0.5, y=0.5
    )
