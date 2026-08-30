"""Answering an assistant's questions, over a real database.

Two things are being tested, and the second matters more than the first.

The projections: a stored shot comes out as the published shape, with units in
the names, the club as a kind rather than a vendor code, and no device
provenance anywhere.

The classification of absence: `empty` when CaddieJ is working and nothing has
happened, `unavailable` when a subsystem is off or broken. Getting that wrong is
how an assistant tells somebody they have not practised today when the truth is
that a file was locked, so most of this file is about it.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from assistant_support import Boundary, boundary, closing, probe

from caddiej.assistant.contract import Availability
from caddiej.domain.coaching import (
    Advice,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
    SeveritySource,
)
from caddiej.domain.events import (
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
)
from caddiej.domain.shot import Club, ClubDelivery, ClubKind, Handedness
from caddiej.persistence.database import Database
from conftest import ShotBuilder, delivery, launch

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


@pytest.fixture
async def subject(tmp_path: Path) -> AsyncIterator[Boundary]:
    async for ready in closing(await boundary(tmp_path)):
        yield ready


@pytest.fixture
async def without_history(tmp_path: Path) -> AsyncIterator[Boundary]:
    async for ready in closing(await boundary(tmp_path, history_enabled=False)):
        yield ready


def an_outcome(
    shot_id: UUID,
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    severity: float = 0.62,
    source: AdviceSource = AdviceSource.MODEL,
) -> AdviceOutcome:
    return AdviceOutcome(
        advice=Advice(
            shot_id=shot_id,
            focus=focus,
            display="FACE OPEN",
            speech="Square the face a little earlier.",
            severity=severity,
            created_at=NOW,
            severity_source=SeveritySource.MEASURED,
            source=source,
        ),
        status=AdviceStatus.OK,
        baseline_shots=12,
    )


# --- the last shot --------------------------------------------------------


async def test_the_last_shot_comes_back_in_the_published_shape(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    shot = make_shot(
        club=ClubKind.IRON,
        vendor_code="I7",
        ball=launch(),
        club_delivery=delivery(),
        handedness=Handedness.RIGHT,
    )
    await subject.record(shot)

    result = await subject.reads.last_shot()

    assert result.availability is Availability.OK
    assert result.shot is not None
    assert result.shot.shot_id == str(shot.shot_id)
    assert result.shot.ordinal == 1
    # The kind, not "I7": a vendor's own code would make every assistant prompt
    # device-specific.
    assert result.shot.club == "iron"
    assert result.shot.handedness == "right"
    assert result.shot.ball is not None
    assert result.shot.ball.speed_mph == pytest.approx(148.2)
    assert result.shot.club_delivery is not None
    # Derived by CaddieJ rather than measured, and the number that explains
    # which way the ball curved.
    assert result.shot.club_delivery.face_to_path_degrees == pytest.approx(3.5)


async def test_a_derived_angle_is_a_number_somebody_would_say(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    """Face to path is a subtraction, and subtraction has a tell.

    A device reporting 1.2 and 1.0 to one decimal gives 0.19999999999999996 in
    binary floating point. It is the right answer, and it is the wrong thing to
    hand to something that reads answers out loud.
    """

    await subject.record(
        make_shot(
            club_delivery=ClubDelivery(path_degrees=1.0, face_to_target_degrees=1.2)
        )
    )

    result = await subject.reads.last_shot()

    assert result.shot is not None
    assert result.shot.club_delivery is not None
    assert result.shot.club_delivery.face_to_path_degrees == 0.2
    # And what the device itself said is passed through untouched: rounding a
    # measurement would be editing the evidence.
    assert result.shot.club_delivery.path_degrees == 1.0


async def test_the_last_shot_is_the_last_one(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    await subject.record(make_shot())
    latest = make_shot(club=ClubKind.PUTTER)
    await subject.record(latest)

    result = await subject.reads.last_shot()

    assert result.shot is not None
    assert result.shot.shot_id == str(latest.shot_id)
    assert result.shot.ordinal == 2


async def test_a_shot_with_no_club_reports_no_club_rather_than_a_guess(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    await subject.record(make_shot(club=None))

    result = await subject.reads.last_shot()

    assert result.shot is not None
    assert result.shot.club is None


async def test_a_device_that_measured_no_club_delivery_reports_none(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    # Most launch monitors measure none of it, and a block of zeroes would read
    # as a player who swings at nothing miles an hour.
    await subject.record(make_shot(ball=launch(), club_delivery=None))

    result = await subject.reads.last_shot()

    assert result.shot is not None
    assert result.shot.club_delivery is None


async def test_no_shots_yet_is_empty_rather_than_unavailable(
    subject: Boundary,
) -> None:
    result = await subject.reads.last_shot()

    assert result.availability is Availability.EMPTY
    assert result.shot is None


async def test_history_switched_off_is_unavailable_rather_than_empty(
    without_history: Boundary,
) -> None:
    # The distinction the whole `availability` field exists for.
    result = await without_history.reads.last_shot()

    assert result.availability is Availability.UNAVAILABLE
    assert result.shot is None


async def test_a_database_that_stops_working_is_unavailable(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    await subject.record(make_shot())
    # As a full disk or a locked file would look. History fails soft and answers
    # None; the façade must not read that as "no golf happened".
    subject.history._available = False

    result = await subject.reads.last_shot()

    assert result.availability is Availability.UNAVAILABLE


async def test_a_query_that_fails_is_unavailable_and_never_empty(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    """The subtlest case, and the one worth the most.

    History fails soft, so a failed query answers with its default — None —
    exactly as a session with no shots in it does. Reporting that as `empty`
    would have an assistant tell somebody they had not practised because a file
    was locked.
    """

    class Failing(Database):
        async def run[T](self, work: object) -> T:  # type: ignore[override]
            raise OSError("disk I/O error")

    ready = await boundary(tmp_path)
    try:
        await ready.record(make_shot())
        # Both connections, because queries are served by the reader and this is
        # about a file that has stopped answering rather than about which handle
        # asked it.
        failing = Failing(tmp_path / "history.sqlite3")
        ready.history._database = failing
        ready.history._reader = failing

        # Not an exception either: an assistant asking a question is never a
        # reason for anything to break.
        assert (await ready.reads.last_shot()).availability is Availability.UNAVAILABLE
        assert (
            await ready.reads.session_stats()
        ).availability is Availability.UNAVAILABLE
        assert (
            await ready.reads.recent_coaching(limit=5)
        ).availability is Availability.UNAVAILABLE
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()


# --- session statistics ---------------------------------------------------


async def test_session_stats_summarise_what_was_measured(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    for speed in (140.0, 150.0, 160.0):
        await subject.record(
            make_shot(club=ClubKind.DRIVER, ball=launch(speed=speed, carry=None))
        )

    result = await subject.reads.session_stats()

    assert result.availability is Availability.OK
    assert result.shots == 3
    assert result.session_id == str(subject.history.session_id)
    speeds = next(metric for metric in result.overall if metric.name == "ball_speed")
    assert speeds.count == 3
    assert speeds.mean == pytest.approx(150.0)
    assert speeds.minimum == pytest.approx(140.0)
    assert speeds.unit == "mph"


async def test_metrics_come_back_in_caddiejs_own_order_every_time(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    # So that two calls over the same shots are comparable, and a golden file is
    # worth having at all.
    await subject.record(make_shot(ball=launch(), club_delivery=delivery()))

    first = await subject.reads.session_stats()
    second = await subject.reads.session_stats()

    assert [metric.name for metric in first.overall] == [
        metric.name for metric in second.overall
    ]
    assert [metric.name for metric in first.overall][:3] == [
        "ball_speed",
        "launch_angle",
        "launch_direction",
    ]


async def test_session_stats_break_down_by_club_busiest_first(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    for _ in range(2):
        await subject.record(make_shot(club=ClubKind.IRON))
    await subject.record(make_shot(club=ClubKind.DRIVER))

    result = await subject.reads.session_stats()

    assert [aggregate.club for aggregate in result.by_club] == ["iron", "driver"]
    assert result.by_club[0].shots == 2


async def test_session_stats_count_the_faults_a_session_was_full_of(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    shot = make_shot()
    await subject.record(shot)
    await subject.history.record_advice(an_outcome(shot.shot_id))

    result = await subject.reads.session_stats()

    assert result.faults == {"face_control": 1}


async def test_a_session_that_has_recorded_nothing_is_empty(
    subject: Boundary,
) -> None:
    # A session row is written with its first shot, so a run nobody has hit
    # anything in genuinely has no session. Normal, not a failure.
    result = await subject.reads.session_stats()

    assert result.availability is Availability.EMPTY
    assert result.shots == 0


async def test_session_stats_are_unavailable_without_history(
    without_history: Boundary,
) -> None:
    assert (
        await without_history.reads.session_stats()
    ).availability is Availability.UNAVAILABLE


async def test_a_session_that_does_not_exist_is_empty(subject: Boundary) -> None:
    result = await subject.reads.session_stats(uuid4())

    assert result.availability is Availability.EMPTY


# --- the state of play ----------------------------------------------------


async def test_the_current_club_is_whatever_the_simulator_said(
    subject: Boundary,
) -> None:
    await subject.play.on_club_selected(
        ClubSelected(club=Club(kind=ClubKind.WEDGE, vendor_code="PW"))
    )

    result = subject.reads.current_club()

    assert result.availability is Availability.OK
    assert result.club == "wedge"


async def test_no_club_named_yet_is_empty_and_never_unavailable(
    subject: Boundary,
) -> None:
    # This state lives in the domain, in this process. There is nothing here to
    # be unavailable, and the normal state before a round starts is "not said".
    result = subject.reads.current_club()

    assert result.availability is Availability.EMPTY
    assert result.club is None


async def test_a_device_silent_about_the_mat_reports_null_not_false(
    subject: Boundary,
) -> None:
    # Many launch monitors report neither ball presence nor readiness, and a
    # device that says nothing is not a device saying no.
    result = subject.reads.current_club()

    assert result.ball_on_mat is None
    assert result.launch_monitor_ready is None
    assert result.handedness is None


async def test_a_ball_on_the_mat_is_reported_as_such(subject: Boundary) -> None:
    await subject.play.on_ball_detected(BallDetected())
    await subject.play.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    result = subject.reads.current_club()

    assert result.ball_on_mat is True
    assert result.launch_monitor_ready is True


# --- recent coaching ------------------------------------------------------


async def test_recent_coaching_reports_faults_without_the_words_by_default(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    # The gate is shut unless the operator opened it: the named fault is a fact
    # about a golf swing, the sentences are a record of somebody being told what
    # they are doing wrong in their own home.
    shot = make_shot()
    await subject.record(shot)
    await subject.history.record_advice(an_outcome(shot.shot_id))

    result = await subject.reads.recent_coaching(limit=5)

    assert result.availability is Availability.OK
    assert result.prose_included is False
    line = result.lines[0]
    assert line.focus == "face_control"
    assert line.severity == pytest.approx(0.62)
    assert line.band == "fault"
    assert line.source == "model"
    assert line.display is None
    assert line.speech is None


async def test_recent_coaching_includes_the_words_when_the_gate_is_open(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    ready = await boundary(tmp_path, include_coaching_prose=True)
    try:
        shot = make_shot()
        await ready.record(shot)
        await ready.history.record_advice(an_outcome(shot.shot_id))

        result = await ready.reads.recent_coaching(limit=5)

        assert result.prose_included is True
        assert result.lines[0].display == "FACE OPEN"
        assert result.lines[0].speech == "Square the face a little earlier."
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


async def test_recent_coaching_bands_a_mild_severity_as_fine(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    # Below 0.25 nothing was said to the player, so an assistant must not
    # present it as a problem.
    shot = make_shot()
    await subject.record(shot)
    await subject.history.record_advice(an_outcome(shot.shot_id, severity=0.1))

    result = await subject.reads.recent_coaching(limit=5)

    assert result.lines[0].band == "fine"


async def test_recent_coaching_returns_the_last_lines_oldest_first(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    focuses = [CoachingFocus.PATH, CoachingFocus.STRIKE, CoachingFocus.SPEED]
    for focus in focuses:
        shot = make_shot()
        await subject.record(shot)
        await subject.history.record_advice(an_outcome(shot.shot_id, focus=focus))

    result = await subject.reads.recent_coaching(limit=2)

    assert [line.focus for line in result.lines] == ["strike", "speed"]


async def test_nothing_coached_yet_is_empty_and_still_says_whether_prose_is_on(
    subject: Boundary,
) -> None:
    result = await subject.reads.recent_coaching(limit=5)

    assert result.availability is Availability.EMPTY
    assert result.lines == []
    # Reported even when there is nothing, so an assistant can tell "the gate is
    # shut" from "nothing was said".
    assert result.prose_included is False


async def test_recent_coaching_is_unavailable_without_history(
    without_history: Boundary,
) -> None:
    assert (
        await without_history.reads.recent_coaching(limit=5)
    ).availability is Availability.UNAVAILABLE


# --- status ---------------------------------------------------------------


async def test_status_reports_the_version_and_the_contract(
    subject: Boundary,
) -> None:
    result = subject.reads.status()

    assert result.availability is Availability.OK
    assert result.caddiej_version == "0.1.0"
    assert result.contract_version >= 1
    assert result.player_name == "Johan"


async def test_status_reports_uptime_from_when_the_facade_was_built(
    subject: Boundary,
) -> None:
    first = subject.reads.status().uptime_seconds
    second = subject.reads.status().uptime_seconds

    assert second > first


async def test_status_names_the_session_history_prepared(
    subject: Boundary, make_shot: ShotBuilder
) -> None:
    # A session is prepared when history opens and its row is written with the
    # first shot, so this is a real identifier from the start while
    # `get_session_stats` is still legitimately empty. The two answers agree
    # once a ball has been hit.
    prepared = subject.reads.status().session_id
    assert prepared == str(subject.history.session_id)

    await subject.record(make_shot())
    stats = await subject.reads.session_stats()

    assert stats.session_id == prepared


async def test_status_reports_no_session_when_history_is_off(
    without_history: Boundary,
) -> None:
    assert without_history.reads.status().session_id is None


async def test_status_reports_the_training_focus(tmp_path: Path) -> None:
    ready = await boundary(tmp_path, preferred_focus=CoachingFocus.PATH)
    try:
        assert ready.reads.status().training_focus == "path"
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


async def test_status_reports_each_subsystem_as_two_booleans(
    tmp_path: Path,
) -> None:
    ready = await boundary(
        tmp_path,
        subsystems={
            "history": probe(True, True),
            "speech": probe(True, False),
            "lighting": probe(False, False),
        },
    )
    try:
        subsystems = ready.reads.status().subsystems

        assert subsystems["speech"].enabled is True
        assert subsystems["speech"].available is False
        assert subsystems["lighting"].enabled is False
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


async def test_capabilities_list_what_will_actually_answer(
    subject: Boundary,
) -> None:
    capabilities = subject.reads.status().capabilities

    assert "get_last_shot" in capabilities
    assert "get_caddiej_status" in capabilities
    assert "set_training_focus" in capabilities


async def test_capabilities_leave_out_what_is_switched_off(
    without_history: Boundary,
) -> None:
    # So an assistant decides what to ask rather than discovering a dead
    # subsystem by being refused by it.
    capabilities = without_history.reads.status().capabilities

    assert "get_last_shot" not in capabilities
    assert "get_session_stats" not in capabilities
    assert "get_recent_coaching" not in capabilities
    # These two answer whatever else is broken.
    assert "get_caddiej_status" in capabilities
    assert "get_current_club" in capabilities


async def test_capabilities_leave_out_the_mutation_when_it_is_not_allowed(
    tmp_path: Path,
) -> None:
    ready = await boundary(tmp_path, mutation_enabled=False)
    try:
        assert "set_training_focus" not in ready.reads.status().capabilities
    finally:
        await ready.coaching.aclose()
        await ready.play.aclose()
        await ready.history.aclose()


async def test_status_answers_when_everything_else_is_broken(
    without_history: Boundary,
) -> None:
    # Its whole job. An assistant that cannot tell "CaddieJ is off" from
    # "CaddieJ has nothing to say" guesses.
    result = without_history.reads.status()

    assert result.availability is Availability.OK
    assert result.subsystems["history"].available is False
