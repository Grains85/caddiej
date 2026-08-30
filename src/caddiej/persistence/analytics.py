"""Session-level statistics over stored shots.

Pure functions over records: no SQL, no clock, no I/O. The arithmetic itself
lives in `domain/statistics.py`, because it is golf rather than storage and
coaching needs the same averages over the shots seen this run. What is left here
is what only a stored session can answer: how the shots break down by club, and
what a dispersion picture would be drawn from.

The aggregates are the ones a golfer would recognise. There is no interpretation
here and no model of ball flight: this counts what was measured and says nothing
about what it means.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from caddiej.domain.shot import ClubKind, Shot
from caddiej.domain.statistics import (
    METRICS,
    Metric,
    MetricSummary,
    club_of,
    summarise_metric,
    summarise_metrics,
)
from caddiej.persistence.records import SessionRecord, StoredShot

# Re-exported so that a caller reviewing a session imports one module. The
# definitions moved to the domain; the names did not.
__all__ = [
    "METRICS",
    "ClubAggregate",
    "DispersionSample",
    "Metric",
    "MetricSummary",
    "SessionAnalytics",
    "aggregate_by_club",
    "club_of",
    "dispersion_samples",
    "summarise_metric",
    "summarise_metrics",
    "summarise_session",
]


@dataclass(frozen=True, slots=True)
class ClubAggregate:
    """Everything measured for one club."""

    club: ClubKind
    shots: int
    metrics: Mapping[str, MetricSummary]


@dataclass(frozen=True, slots=True)
class DispersionSample:
    """One shot's worth of the data a dispersion picture is drawn from.

    Deliberately not a coordinate. Turning launch direction and spin into a
    landing position needs a ball-flight model, and CaddieJ does not have one:
    GSPro does, and does not tell us where the ball finished. So this returns
    what was measured and leaves plotting to whatever knows the shape of the
    surface it is plotting on.
    """

    shot_id: UUID
    ordinal: int
    club: ClubKind
    carry_yards: float | None
    launch_direction_degrees: float
    # All three, because devices disagree about which they report: some send
    # side spin, others a total and a tilted axis that amounts to the same
    # thing. Whoever draws the picture can use whichever arrived.
    total_spin_rpm: float | None
    side_spin_rpm: float | None
    spin_axis_degrees: float | None


@dataclass(frozen=True, slots=True)
class SessionAnalytics:
    """What a session amounted to."""

    session_id: UUID
    label: str | None
    started_at: datetime
    ended_at: datetime | None
    shots: int
    first_shot_at: datetime | None
    last_shot_at: datetime | None
    overall: Mapping[str, MetricSummary]
    by_club: tuple[ClubAggregate, ...]
    dispersion: tuple[DispersionSample, ...]


def aggregate_by_club(shots: Sequence[StoredShot]) -> tuple[ClubAggregate, ...]:
    """Group shots by club, busiest club first."""

    grouped: dict[ClubKind, list[Shot]] = {}
    for stored in shots:
        grouped.setdefault(club_of(stored.shot), []).append(stored.shot)

    aggregates = [
        ClubAggregate(
            club=club, shots=len(club_shots), metrics=summarise_metrics(club_shots)
        )
        for club, club_shots in grouped.items()
    ]
    # Busiest first because that is the club a session was about, with the name
    # as a tiebreak so the order never depends on insertion order.
    aggregates.sort(key=lambda aggregate: (-aggregate.shots, aggregate.club.value))
    return tuple(aggregates)


def dispersion_samples(shots: Sequence[StoredShot]) -> tuple[DispersionSample, ...]:
    return tuple(
        DispersionSample(
            shot_id=stored.shot.shot_id,
            ordinal=stored.ordinal,
            club=club_of(stored.shot),
            carry_yards=stored.shot.ball.carry_yards,
            launch_direction_degrees=stored.shot.ball.launch_direction_degrees,
            total_spin_rpm=stored.shot.ball.total_spin_rpm,
            side_spin_rpm=stored.shot.ball.side_spin_rpm,
            spin_axis_degrees=stored.shot.ball.spin_axis_degrees,
        )
        for stored in shots
        if stored.shot.ball is not None
    )


def summarise_session(
    session: SessionRecord, shots: Sequence[StoredShot]
) -> SessionAnalytics:
    """Everything deterministic that can be said about one session's shots."""

    observed = sorted(stored.shot.source.observed_at for stored in shots)
    return SessionAnalytics(
        session_id=session.session_id,
        label=session.label,
        started_at=session.started_at,
        ended_at=session.ended_at,
        shots=len(shots),
        first_shot_at=observed[0] if observed else None,
        last_shot_at=observed[-1] if observed else None,
        overall=summarise_metrics([stored.shot for stored in shots]),
        by_club=aggregate_by_club(shots),
        dispersion=dispersion_samples(shots),
    )
