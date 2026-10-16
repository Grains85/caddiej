"""Aggregating what was measured. Arithmetic, and nothing else.

Pure functions over shots: no SQL, no clock, no I/O. Two reasons for that. The
arithmetic can be tested against numbers worked out by hand with no database in
the way, and SQLite is never asked to compute an average that Python would round
differently — one implementation, one answer.

This lives in the domain rather than beside the database because it is golf, not
storage. Session history summarises stored shots with it, and coaching uses the
same functions on the shots seen so far this run to work out what normal looks
like for the player standing on the mat. Both are consumers; neither owns it.

There is no interpretation here and no model of ball flight: this counts what was
measured and says nothing about what it means.
"""

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from caddiej.domain.shot import ClubKind, Shot


@dataclass(frozen=True, slots=True)
class Metric:
    """A number worth aggregating, and how to get it off a shot."""

    name: str
    unit: str
    extract: Callable[[Shot], float | None]


def _ball(get: Callable[..., float | None]) -> Callable[[Shot], float | None]:
    return lambda shot: None if shot.ball is None else get(shot.ball)


def _club(get: Callable[..., float | None]) -> Callable[[Shot], float | None]:
    return lambda shot: None if shot.club_delivery is None else get(shot.club_delivery)


# Order is presentation order, and it is fixed: two runs over the same shots
# produce the same summary in the same sequence, which is what makes a summary
# diffable and a test readable.
METRICS: tuple[Metric, ...] = (
    Metric("ball_speed", "mph", _ball(lambda ball: ball.speed_mph)),
    Metric("launch_angle", "degrees", _ball(lambda ball: ball.launch_angle_degrees)),
    Metric(
        "launch_direction",
        "degrees",
        _ball(lambda ball: ball.launch_direction_degrees),
    ),
    Metric("carry", "yards", _ball(lambda ball: ball.carry_yards)),
    Metric("total_spin", "rpm", _ball(lambda ball: ball.total_spin_rpm)),
    Metric("back_spin", "rpm", _ball(lambda ball: ball.back_spin_rpm)),
    Metric("side_spin", "rpm", _ball(lambda ball: ball.side_spin_rpm)),
    Metric("spin_axis", "degrees", _ball(lambda ball: ball.spin_axis_degrees)),
    Metric("club_speed", "mph", _club(lambda club: club.speed_mph)),
    Metric(
        "angle_of_attack",
        "degrees",
        _club(lambda club: club.angle_of_attack_degrees),
    ),
    Metric("club_path", "degrees", _club(lambda club: club.path_degrees)),
    Metric(
        "face_to_target",
        "degrees",
        _club(lambda club: club.face_to_target_degrees),
    ),
    # Derived by the domain, not measured by anything. It is the number that
    # explains which way the ball curved, so it is worth aggregating even though
    # no device reports it.
    Metric("face_to_path", "degrees", _club(lambda club: club.face_to_path_degrees)),
)


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """One measurement across a set of shots.

    `count` is the number of shots that carried this measurement, not the number
    of shots in the set. A device that reports carry for drives and not for putts
    would otherwise appear to have hit soft drives.
    """

    name: str
    unit: str
    count: int
    mean: float
    median: float
    minimum: float
    maximum: float
    standard_deviation: float | None

    @property
    def spread(self) -> float:
        return self.maximum - self.minimum


def _summarise_metric(metric: Metric, shots: Sequence[Shot]) -> MetricSummary | None:
    """Aggregate one measurement, or None if no shot carried it.

    Private: `summarise_metrics` is the whole of what anything outside this
    module has ever wanted, and a public single-metric variant was two more
    names in two `__all__` lists for no caller.
    """

    values = [
        value for value in (metric.extract(shot) for shot in shots) if value is not None
    ]
    if not values:
        return None
    return MetricSummary(
        name=metric.name,
        unit=metric.unit,
        count=len(values),
        mean=statistics.fmean(values),
        median=statistics.median(values),
        minimum=min(values),
        maximum=max(values),
        # Sample rather than population: an evening's shots are a sample of how
        # someone swings, not the whole of it. Undefined for one shot, and a
        # zero there would read as perfect consistency.
        standard_deviation=statistics.stdev(values) if len(values) > 1 else None,
    )


def summarise_metrics(shots: Sequence[Shot]) -> dict[str, MetricSummary]:
    summaries = ((metric.name, _summarise_metric(metric, shots)) for metric in METRICS)
    return {name: summary for name, summary in summaries if summary is not None}


def club_of(shot: Shot) -> ClubKind:
    """Which club a shot counts towards.

    A shot hit before the simulator named a club is not thrown away; it is
    counted as unknown, which is what it is.
    """

    return ClubKind.UNKNOWN if shot.club is None else shot.club.kind
