"""Diagnostic ball-flight fitting for the HUD chart.

This is not floor geometry. Points are auto-fitted into a 0..1 panel so the
overlay can draw a trace without treating GSPro world axes as physical floor
axes, and without touching FloorCalibration.
"""

from collections.abc import Sequence

from caddiej.domain.observation import BallPathPoint, WorldPoint
from caddiej.floor.state import FlightDrawPoint

_PADDING = 0.08


def relative_ball_path(
    path: Sequence[BallPathPoint],
    start: WorldPoint | None,
) -> tuple[BallPathPoint, ...]:
    """Subtract BallStartPos from each sample. Missing start leaves values raw."""

    if start is None:
        return tuple(path)
    return tuple(
        BallPathPoint(
            time=point.time,
            x=point.x - start.x,
            y=point.y - start.y,
            z=point.z - start.z,
        )
        for point in path
    )


def fit_ball_flight(path: Sequence[BallPathPoint]) -> tuple[FlightDrawPoint, ...]:
    """Map relative x/z onto a padded 0..1 chart, preserving aspect ratio.

    x vs z is a plot choice for a diagnostic line, not a claim about which
    GSPro axis faces the player. A later physical mapping does not use this.
    """

    if not path:
        return ()
    xs = [point.x for point in path]
    zs = [point.z for point in path]
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)
    span = max(max_x - min_x, max_z - min_z, 1e-9)
    mid_x = (min_x + max_x) / 2.0
    mid_z = (min_z + max_z) / 2.0
    usable = 1.0 - 2.0 * _PADDING
    scale = usable / span
    return tuple(
        FlightDrawPoint(
            x=0.5 + (point.x - mid_x) * scale,
            y=0.5 + (point.z - mid_z) * scale,
        )
        for point in path
    )
