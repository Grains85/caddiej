"""Answering questions, and the projections that make the answers safe.

Every read an assistant can perform is a method here, and every one of them
turns an internal object into a hand-written contract model before returning.
That translation is the boundary. It is deliberately not in `contract.py`,
because the out-of-process MCP server imports that module and it must stay free
of any dependency on the domain, persistence or coaching — otherwise a
translator would carry a database driver.

Three properties hold everywhere below.

**Nothing raises.** Every service this calls already fails soft, and where one
could still surprise us the result is reported as `UNAVAILABLE` rather than
propagated. An assistant asking a question is never a reason for anything to
break, and a traceback crossing this boundary would be both a leak and a lie
about severity.

**Nothing waits on the event path.** These reads go to SQLite on its own thread
or read an attribute. None of them publishes, subscribes, or touches the bus,
whose single worker is busy with the floor.

**Absence is classified.** `EMPTY` and `UNAVAILABLE` are decided here, once,
by asking the subsystem whether it is working before concluding that a null
means "no golf happened". Getting this wrong is how an assistant tells somebody
they have not practised when the truth is that a file was locked.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from time import monotonic as default_monotonic
from typing import Protocol
from uuid import UUID

from caddiej.assistant.contract import (
    CONTRACT_VERSION,
    Availability,
    BallPayload,
    ClubDeliveryPayload,
    ClubStatsPayload,
    CoachingLinePayload,
    CurrentClubResult,
    LastShotResult,
    MetricPayload,
    RecentCoachingResult,
    SessionStatsResult,
    ShotPayload,
    StatusResult,
    SubsystemPayload,
)
from caddiej.coaching.service import AnalysisService
from caddiej.domain.coaching import CoachingFocus, band_for
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import BallLaunch, ClubDelivery, Detection, Handedness
from caddiej.domain.statistics import METRICS, MetricSummary
from caddiej.persistence.analytics import SessionAnalytics
from caddiej.persistence.records import StoredAdvice, StoredShot
from caddiej.persistence.service import HistoryService

logger = logging.getLogger(__name__)

SubsystemProbe = Callable[[], SubsystemPayload]
"""How a subsystem reports itself: two booleans, in the published vocabulary.

A callable rather than a service reference, and two booleans rather than a
health object, so that the assistant boundary reports on subsystems it knows
nothing about and no internal health shape can leak through it by growing a
field. The HUD's `/healthz` takes the opposite approach and publishes whole
health dataclasses, which is why its shape is not something anybody can depend
on.
"""


class HealthReport(Protocol):
    """The two fields every CaddieJ subsystem's health object has in common."""

    @property
    def enabled(self) -> bool: ...

    @property
    def available(self) -> bool: ...


def subsystem_probe(report: Callable[[], HealthReport]) -> SubsystemProbe:
    """Narrow a subsystem's health report down to what may be published.

    The narrowing happens here, once, rather than at each call site, so a
    subsystem that grows a `last_error` or a token count does not thereby grow
    the assistant API.
    """

    def presence() -> SubsystemPayload:
        health = report()
        return SubsystemPayload(enabled=health.enabled, available=health.available)

    return presence


class ReadFacade:
    """Every question an assistant may ask, and no way to change anything.

    Holds no mutable reference it could write through: history and coaching are
    used for their read methods, and `PlayFlow` is asked four properties. That
    is what makes "reads cannot mutate" a fact about the object graph rather
    than a naming convention.
    """

    def __init__(
        self,
        history: HistoryService,
        coaching: AnalysisService,
        play: PlayFlow,
        version: str,
        subsystems: Mapping[str, SubsystemProbe] | None = None,
        player_name: str | None = None,
        include_coaching_prose: bool = False,
        mutation_enabled: bool = False,
        monotonic: Callable[[], float] = default_monotonic,
    ) -> None:
        self._history = history
        self._coaching = coaching
        self._play = play
        self._version = version
        self._subsystems = dict(subsystems or {})
        self._player_name = player_name or None
        # Off unless the operator opened it. Coaching prose is a record of
        # somebody being told what they are doing wrong in their own home, and
        # handing that to a hosted assistant is a different decision from
        # handing over a launch angle — the same distinction the frame switch
        # draws for camera stills.
        self._include_prose = include_coaching_prose
        self._mutation_enabled = mutation_enabled
        self._monotonic = monotonic
        self._started = monotonic()

    # --- status -----------------------------------------------------------

    def status(self) -> StatusResult:
        """What CaddieJ is, and which of its parts would answer.

        Synchronous and touching nothing that can fail, because this is the tool
        an assistant reaches for when something else has already gone wrong.
        """

        return StatusResult(
            contract_version=CONTRACT_VERSION,
            caddiej_version=self._version,
            uptime_seconds=round(self._monotonic() - self._started, 3),
            session_id=_uuid(self._history.session_id),
            player_name=self._player_name,
            training_focus=_focus_name(self._coaching.preferred_focus),
            subsystems={name: probe() for name, probe in self._subsystems.items()},
            capabilities=self._capabilities(),
        )

    def _capabilities(self) -> list[str]:
        """Which tools will return something other than `unavailable`.

        Reported so an assistant can decide what to ask rather than learning by
        being refused. Ordered as the tools are documented, not by availability,
        so the list reads the same way twice.
        """

        history = self._history.is_available
        capable = ["get_caddiej_status", "get_current_club"]
        if history:
            capable.extend(
                ["get_last_shot", "get_session_stats", "get_recent_coaching"]
            )
        if self._mutation_enabled:
            capable.append("set_training_focus")
        return sorted(capable)

    # --- the state of play ------------------------------------------------

    def current_club(self) -> CurrentClubResult:
        """What is in play, and whether anybody is about to hit anything.

        Never `UNAVAILABLE`: this state lives in the domain, in the same process,
        and there is nothing here to be unavailable. A club of null is `EMPTY`,
        which is the honest answer before a round starts.
        """

        club = self._play.current_club
        ball = self._play.ball
        return CurrentClubResult(
            availability=Availability.OK if club is not None else Availability.EMPTY,
            club=None if club is None else club.kind.value,
            # Tri-state to a nullable boolean: a device that reports nothing
            # about the mat is not a device reporting an empty one.
            ball_on_mat=None
            if ball is Detection.UNKNOWN
            else ball is Detection.PRESENT,
            launch_monitor_ready=self._play.ready,
            handedness=(
                None
                if self._play.handedness is Handedness.UNKNOWN
                else self._play.handedness.value
            ),
        )

    # --- history ----------------------------------------------------------

    def _before_reading(self) -> int:
        """A mark to compare after a query, so a failure is not read as a null.

        History fails soft: a locked file or a full disk makes a query answer
        with its default rather than raise, so "no rows" and "the query failed"
        arrive identically. That is right for a statistics view and wrong here,
        because those are precisely the two things an assistant must not
        conflate. Hence the counter.

        Two assistants reading at once could have one see the other's failure and
        report `unavailable` when its own query was fine. That is the harmless
        direction: it says "I could not tell you" instead of "you did not
        practise", and the cautious answer is cheap while the wrong one is not.
        """

        return self._history.read_failures

    def _read_failed(self, mark: int) -> bool:
        return self._history.read_failures != mark

    async def last_shot(self, session_id: UUID | None = None) -> LastShotResult:
        if not self._history.is_available:
            return LastShotResult(availability=Availability.UNAVAILABLE)

        mark = self._before_reading()
        stored = await self._history.last_shot(session_id)
        if self._read_failed(mark):
            return LastShotResult(availability=Availability.UNAVAILABLE)
        if stored is None:
            return LastShotResult(availability=Availability.EMPTY)
        return LastShotResult(availability=Availability.OK, shot=_shot(stored))

    async def session_stats(self, session_id: UUID | None = None) -> SessionStatsResult:
        if not self._history.is_available:
            return SessionStatsResult(availability=Availability.UNAVAILABLE)

        mark = self._before_reading()
        analytics = await self._history.analytics(session_id)
        faults = (
            {} if analytics is None else await self._history.focus_counts(session_id)
        )
        if self._read_failed(mark):
            return SessionStatsResult(availability=Availability.UNAVAILABLE)
        if analytics is None:
            # No session row yet, which is the normal state until the first shot
            # of a run is written. Not a failure, and not worth a null nobody
            # can interpret.
            return SessionStatsResult(availability=Availability.EMPTY)

        return SessionStatsResult(
            availability=Availability.OK,
            session_id=str(analytics.session_id),
            label=analytics.label,
            started_at=analytics.started_at,
            shots=analytics.shots,
            first_shot_at=analytics.first_shot_at,
            last_shot_at=analytics.last_shot_at,
            overall=_metrics(analytics.overall),
            by_club=_by_club(analytics),
            faults=faults,
        )

    async def recent_coaching(
        self, limit: int, session_id: UUID | None = None
    ) -> RecentCoachingResult:
        if not self._history.is_available:
            return RecentCoachingResult(availability=Availability.UNAVAILABLE)

        mark = self._before_reading()
        stored = await self._history.recent_advice(limit, session_id)
        if self._read_failed(mark):
            return RecentCoachingResult(availability=Availability.UNAVAILABLE)
        if not stored:
            return RecentCoachingResult(
                availability=Availability.EMPTY,
                prose_included=self._include_prose,
            )
        return RecentCoachingResult(
            availability=Availability.OK,
            prose_included=self._include_prose,
            lines=[_coaching_line(row, self._include_prose) for row in stored],
        )


# --- projections --------------------------------------------------------
#
# Written out by hand, field by field, on purpose. A generic converter over
# dataclass fields would be shorter and would publish every field anybody ever
# adds to an internal model, which is the failure this whole layer exists to
# prevent.


def _uuid(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _focus_name(focus: CoachingFocus | None) -> str | None:
    return None if focus is None else focus.value


def _shot(stored: StoredShot) -> ShotPayload:
    shot = stored.shot
    delivery = shot.club_delivery
    return ShotPayload(
        shot_id=str(shot.shot_id),
        ordinal=stored.ordinal,
        observed_at=shot.source.observed_at,
        # The kind only. A vendor's own code for a club is the launch monitor's
        # vocabulary, and letting it out here would make every assistant
        # prompt device-specific.
        club=None if shot.club is None else shot.club.kind.value,
        handedness=shot.handedness.value,
        ball=None if shot.ball is None else _ball(shot.ball),
        club_delivery=(
            None if delivery is None or delivery.is_empty else _delivery(delivery)
        ),
    )


def _ball(ball: BallLaunch) -> BallPayload:
    return BallPayload(
        speed_mph=ball.speed_mph,
        launch_angle_degrees=ball.launch_angle_degrees,
        launch_direction_degrees=ball.launch_direction_degrees,
        carry_yards=ball.carry_yards,
        total_spin_rpm=ball.total_spin_rpm,
        back_spin_rpm=ball.back_spin_rpm,
        side_spin_rpm=ball.side_spin_rpm,
        spin_axis_degrees=ball.spin_axis_degrees,
    )


def _delivery(club: ClubDelivery) -> ClubDeliveryPayload:
    return ClubDeliveryPayload(
        speed_mph=club.speed_mph,
        angle_of_attack_degrees=club.angle_of_attack_degrees,
        path_degrees=club.path_degrees,
        face_to_target_degrees=club.face_to_target_degrees,
        face_to_path_degrees=_derived_angle(club.face_to_path_degrees),
        loft_degrees=club.loft_degrees,
        impact_horizontal_mm=club.impact_horizontal_mm,
        impact_vertical_mm=club.impact_vertical_mm,
    )


def _derived_angle(value: float | None) -> float | None:
    """Round a computed angle back to the precision a device reports.

    Only the derived one needs it. Subtracting two figures a launch monitor gave
    to one decimal produces 0.19999999999999996 in binary floating point, which
    is the right number and the wrong way to hand it to something that reads
    answers out loud. Measured values are passed through untouched: rounding
    what a device actually said would be editing the evidence.
    """

    return None if value is None else round(value, 2)


def _metrics(summaries: Mapping[str, MetricSummary]) -> list[MetricPayload]:
    """In CaddieJ's own metric order, not the mapping's.

    So that two calls over the same shots produce the same list in the same
    sequence, which is what makes an answer comparable and a golden file worth
    having.
    """

    ordered = (summaries.get(metric.name) for metric in METRICS)
    return [_metric(summary) for summary in ordered if summary is not None]


def _metric(summary: MetricSummary) -> MetricPayload:
    return MetricPayload(
        name=summary.name,
        unit=summary.unit,
        count=summary.count,
        mean=summary.mean,
        median=summary.median,
        minimum=summary.minimum,
        maximum=summary.maximum,
        standard_deviation=summary.standard_deviation,
    )


def _by_club(analytics: SessionAnalytics) -> list[ClubStatsPayload]:
    return [
        ClubStatsPayload(
            club=aggregate.club.value,
            shots=aggregate.shots,
            metrics=_metrics(aggregate.metrics),
        )
        for aggregate in analytics.by_club
    ]


def _coaching_line(stored: StoredAdvice, include_prose: bool) -> CoachingLinePayload:
    advice = stored.outcome.advice
    return CoachingLinePayload(
        shot_id=str(advice.shot_id),
        focus=advice.focus.value,
        severity=advice.severity,
        band=band_for(advice.severity).value,
        source=advice.source.value,
        created_at=advice.created_at,
        # Note what is never here whether the gate is open or not: the clip
        # path. It is a location on the operator's disk, and an assistant that
        # could read one would be a file oracle rather than a caddie.
        display=(advice.display or None) if include_prose else None,
        speech=(advice.speech or None) if include_prose else None,
    )


def coaching_lines(
    stored: Sequence[StoredAdvice], include_prose: bool = False
) -> list[CoachingLinePayload]:
    """Project stored advice, for anything that already has the rows."""

    return [_coaching_line(row, include_prose) for row in stored]
