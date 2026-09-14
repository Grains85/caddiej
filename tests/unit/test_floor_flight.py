"""Diagnostic ball-flight fitting, kept off the physical floor plane."""

from caddiej.domain.observation import BallPathPoint, WorldPoint
from caddiej.floor.flight import fit_ball_flight, relative_ball_path


def test_path_is_normalized_to_the_start_position() -> None:
    start = WorldPoint(x=10.0, y=1.0, z=20.0)
    path = (
        BallPathPoint(time=0.0, x=10.0, y=1.0, z=20.0),
        BallPathPoint(time=0.5, x=14.0, y=3.0, z=28.0),
    )

    relative = relative_ball_path(path, start)

    assert relative[0] == BallPathPoint(time=0.0, x=0.0, y=0.0, z=0.0)
    assert relative[1] == BallPathPoint(time=0.5, x=4.0, y=2.0, z=8.0)


def test_a_fitted_trace_stays_inside_the_chart() -> None:
    path = (
        BallPathPoint(time=0.0, x=0.0, y=0.0, z=0.0),
        BallPathPoint(time=1.0, x=10.0, y=1.0, z=4.0),
    )

    fitted = fit_ball_flight(path)

    assert len(fitted) == 2
    for point in fitted:
        assert 0.0 <= point.x <= 1.0
        assert 0.0 <= point.y <= 1.0


def test_an_empty_path_fits_to_nothing() -> None:
    assert fit_ball_flight(()) == ()
