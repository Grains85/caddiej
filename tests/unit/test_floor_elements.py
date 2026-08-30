"""Marker, club path, and metrics state: the floor's authoritative geometry."""

import math

import pytest

from caddiej.floor import (
    BallMarker,
    ClubPath,
    FloorFlourish,
    FloorPoint,
    FloorService,
    FloorState,
    MarkerVariant,
    ShotMetrics,
)


def test_a_fresh_floor_has_a_hidden_marker_and_nothing_else() -> None:
    state = FloorState()

    assert state.marker.visible is False
    assert state.metrics is None
    assert state.club_path is None
    assert state.flourish is None


def test_showing_the_marker_places_it_and_records_the_club() -> None:
    service = FloorService()

    state = service.show_marker(FloorPoint(x=0.25, y=0.8), MarkerVariant.DRIVER)

    assert state.marker.visible is True
    assert state.marker.position == FloorPoint(x=0.25, y=0.8)
    assert state.marker.variant is MarkerVariant.DRIVER


def test_marker_defaults_to_the_normal_variant() -> None:
    service = FloorService()

    assert service.show_marker(FloorPoint(x=0.5, y=0.5)).marker.variant is (
        MarkerVariant.NORMAL
    )


@pytest.mark.parametrize("variant", list(MarkerVariant))
def test_every_marker_variant_round_trips(variant: MarkerVariant) -> None:
    service = FloorService()

    state = service.show_marker(FloorPoint(x=0.4, y=0.6), variant)

    assert state.marker.variant is variant


def test_hiding_the_marker_keeps_where_it_was() -> None:
    # The marker is hidden once the ball is detected, and the overlay animates
    # out from where the ball actually was.
    service = FloorService()
    service.show_marker(FloorPoint(x=0.3, y=0.7), MarkerVariant.PUTTER)

    hidden = service.hide_marker()

    assert hidden.marker.visible is False
    assert hidden.marker.position == FloorPoint(x=0.3, y=0.7)
    assert hidden.marker.variant is MarkerVariant.PUTTER


def test_show_and_hide_are_separate_versions() -> None:
    service = FloorService()
    seen: list[FloorState] = []
    service.add_listener(seen.append)

    service.show_marker(FloorPoint(x=0.5, y=0.5))
    service.hide_marker()

    assert [state.marker.visible for state in seen] == [True, False]
    assert [state.version for state in seen] == [1, 2]


def test_hiding_an_already_hidden_marker_changes_nothing() -> None:
    service = FloorService()
    seen: list[FloorState] = []
    service.add_listener(seen.append)

    service.hide_marker()

    assert seen == []
    assert service.state.version == 0


@pytest.mark.parametrize(
    "x,y",
    [(-0.01, 0.5), (1.01, 0.5), (0.5, -0.01), (0.5, 1.01)],
)
def test_a_marker_off_the_surface_is_rejected(x: float, y: float) -> None:
    # Catches the unit confusion of passing pixels where a fraction belongs.
    with pytest.raises(ValueError, match="on the surface"):
        BallMarker(position=FloorPoint(x=x, y=y), visible=True)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_coordinates_are_rejected(bad: float) -> None:
    # These serialise to tokens that are not valid JSON, so they would reach the
    # browser as unparseable text rather than as a visible error.
    with pytest.raises(ValueError, match="finite"):
        FloorPoint(x=bad, y=0.5)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_metrics_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ShotMetrics(ball_speed_mph=bad, launch_angle_degrees=10.0, carry_yards=200.0)


def test_metrics_are_set_and_cleared() -> None:
    service = FloorService()
    metrics = ShotMetrics(
        ball_speed_mph=148.2, launch_angle_degrees=12.6, carry_yards=268.4
    )

    assert service.set_metrics(metrics).metrics == metrics
    assert service.clear_metrics().metrics is None


def test_club_path_is_set_and_cleared() -> None:
    service = FloorService()
    path = ClubPath(start=FloorPoint(x=0.5, y=0.7), angle_degrees=0.0, length=0.3)

    assert service.set_club_path(path).club_path == path
    assert service.clear_club_path().club_path is None


def test_a_straight_path_runs_down_range() -> None:
    # Zero degrees is straight down-range. On the default 1 m × 1 m floor
    # that is towards the back of the surface, so y decreases by the length.
    path = ClubPath(start=FloorPoint(x=0.5, y=0.8), angle_degrees=0.0, length=0.3)

    assert path.end.x == pytest.approx(0.5)
    assert path.end.y == pytest.approx(0.5)


def test_a_positive_angle_goes_right_and_a_negative_one_goes_left() -> None:
    start = FloorPoint(x=0.5, y=0.8)

    right = ClubPath(start=start, angle_degrees=90.0, length=0.2).end
    left = ClubPath(start=start, angle_degrees=-90.0, length=0.2).end

    assert right.x == pytest.approx(0.7)
    assert right.y == pytest.approx(0.8)
    assert left.x == pytest.approx(0.3)
    assert left.y == pytest.approx(0.8)


def test_path_length_is_preserved_at_an_arbitrary_angle() -> None:
    # On the default 1 m × 1 m floor, metres and surface units coincide, so
    # the drawn length in the unit square is the physical length.
    start = FloorPoint(x=0.5, y=0.6)
    path = ClubPath(start=start, angle_degrees=37.0, length=0.25)

    end = path.end
    distance = math.hypot(end.x - start.x, end.y - start.y)

    assert distance == pytest.approx(0.25)


def test_a_path_may_leave_the_surface() -> None:
    # A long path from near an edge legitimately runs off the floor; the
    # renderer clips it. Only marker placement is constrained to the surface.
    path = ClubPath(start=FloorPoint(x=0.5, y=0.1), angle_degrees=0.0, length=0.8)

    assert path.end.y == pytest.approx(-0.7)


def test_a_full_turn_returns_to_the_start() -> None:
    start = FloorPoint(x=0.4, y=0.4)
    path = ClubPath(start=start, angle_degrees=360.0, length=0.2)

    assert path.end.x == pytest.approx(start.x)
    assert path.end.y == pytest.approx(start.y - 0.2)


def test_a_negative_path_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        ClubPath(start=FloorPoint(x=0.5, y=0.5), angle_degrees=0.0, length=-0.1)


def test_a_zero_length_path_is_allowed() -> None:
    path = ClubPath(start=FloorPoint(x=0.5, y=0.5), angle_degrees=45.0, length=0.0)

    assert path.end == FloorPoint(x=0.5, y=0.5)


def test_a_celebration_is_a_layer_over_whatever_the_round_put_down() -> None:
    # The property that makes an effect's reset clean: one field to clear, and
    # clearing it cannot disturb the marker, the numbers or the coaching line.
    service = FloorService()
    service.show_marker(FloorPoint(x=0.5, y=0.7), MarkerVariant.DRIVER)
    service.set_metrics(ShotMetrics(ball_speed_mph=148.2, launch_angle_degrees=12.6))
    before = service.state

    service.show_flourish(FloorFlourish(cue="birdie", label="BIRDIE"))
    during = service.state

    assert during.flourish is not None
    assert during.marker == before.marker
    assert during.metrics == before.metrics

    after = service.clear_flourish()

    assert after.flourish is None
    assert after.marker == before.marker
    assert after.metrics == before.metrics


def test_clearing_a_celebration_that_is_not_showing_is_not_a_new_version() -> None:
    # The director resets unconditionally, so this happens on every effect whose
    # floor channel was switched off. It must not churn the feed.
    service = FloorService()
    version = service.state.version

    service.clear_flourish()

    assert service.state.version == version


@pytest.mark.parametrize(
    ("cue", "label", "intensity", "seconds"),
    [
        ("", "BIRDIE", 0.5, 1.0),
        ("birdie", "", 0.5, 1.0),
        ("birdie", "BIRDIE", -0.1, 1.0),
        ("birdie", "BIRDIE", 1.1, 1.0),
        ("birdie", "BIRDIE", 0.5, 0.0),
        ("birdie", "BIRDIE", 0.5, math.inf),
    ],
)
def test_an_impossible_celebration_is_rejected(
    cue: str, label: str, intensity: float, seconds: float
) -> None:
    with pytest.raises(ValueError):
        FloorFlourish(cue=cue, label=label, intensity=intensity, seconds=seconds)
