"""Recording shots without making anybody wait, and surviving it going wrong.

Every test gets its own database under `tmp_path`. The doubles here subclass the
real `Database` rather than replacing it, so the SQL still runs: what they change
is when it runs and whether it works.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.events import ShotCompleted
from caddiej.domain.shot import ClubKind
from caddiej.persistence.database import Database
from caddiej.persistence.service import HistoryService, build_history_service
from conftest import ShotBuilder, launch

pytestmark = pytest.mark.asyncio


class StallingDatabase(Database):
    """A database whose statements wait until a test lets them through."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.stalling = False
        self._release = asyncio.Event()

    async def run[ResultType](self, work: Callable[..., ResultType]) -> ResultType:
        if self.stalling:
            await self._release.wait()
        return await super().run(work)

    async def release(self) -> None:
        self.stalling = False
        self._release.set()


class FailingDatabase(Database):
    """A database that has stopped working, as a full disk would."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.failing = False

    async def run[ResultType](self, work: Callable[..., ResultType]) -> ResultType:
        if self.failing:
            raise OSError("disk I/O error")
        return await super().run(work)


async def service_for(
    database: Database, queue_size: int = 256, player: str | None = "Johan"
) -> HistoryService:
    service = HistoryService(
        database=database, player_name=player, queue_size=queue_size
    )
    await service.open()
    return service


async def running(tmp_path: Path) -> HistoryService:
    service = build_history_service(enabled=True, path=tmp_path / "history.sqlite3")
    await service.open()
    return service


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[HistoryService]:
    subject = build_history_service(
        enabled=True, path=tmp_path / "history.sqlite3", player_name="Johan"
    )
    await subject.open()
    yield subject
    await subject.aclose()


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for the writer to catch up.

    Polled rather than counted in event-loop turns, because the write really
    happens on another thread — which is the whole point of the design.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError("the writer never caught up")
        await asyncio.sleep(0.005)


# --- the happy path -------------------------------------------------------


async def test_opening_starts_a_session(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    assert service.session_id is not None
    assert service.is_available is True

    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 1)

    sessions = await service.sessions()
    assert [session.session_id for session in sessions] == [service.session_id]


async def test_a_run_that_recorded_nothing_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    # Otherwise every start of CaddieJ, and every tool that only reads, would
    # add an empty session to the list somebody is trying to review.
    service = await running(tmp_path)
    assert service.session_id is not None
    await service.aclose()

    reader = await running(tmp_path)
    assert await reader.sessions() == []
    await reader.aclose()


async def test_a_completed_shot_is_stored(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = make_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await until(lambda: service.shots_recorded == 1)

    stored = await service.shots()
    assert [read.shot.shot_id for read in stored] == [shot.shot_id]
    assert stored[0].player_name == "Johan"
    assert service.shots_recorded == 1


async def test_shots_are_numbered_in_the_order_they_were_hit(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    for _ in range(3):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 3)

    assert [read.ordinal for read in await service.shots()] == [1, 2, 3]


async def test_a_backlog_is_written_in_one_go(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # Catching up is the only time a backlog exists, and it is exactly when one
    # commit rather than twenty matters.
    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    database.stalling = True
    for _ in range(5):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))
        await asyncio.sleep(0)

    await database.release()
    await until(lambda: service.shots_recorded == 5)

    assert service.shots_recorded == 5
    await service.aclose()


async def test_closing_records_how_the_session_ended(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    service = await running(tmp_path)
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    session_id = service.session_id
    assert session_id is not None

    await service.aclose()

    # Reopened, because the point is that it is on disk.
    reader = await running(tmp_path)
    sessions = {session.session_id: session for session in await reader.sessions()}
    assert sessions[session_id].ended_at is not None
    assert sessions[session_id].shot_count == 1
    await reader.aclose()


async def test_queued_shots_are_written_before_shutdown_finishes(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    service = await running(tmp_path)
    for _ in range(4):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))

    # No settling: closing has to drain what is still queued.
    await service.aclose()

    reader = await running(tmp_path)
    sessions = await reader.sessions()
    assert [session.shot_count for session in sessions] == [4]
    await reader.aclose()


# --- nothing on the event path waits for a disk ---------------------------


async def test_recording_a_shot_does_not_wait_for_the_database(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # The event bus dispatches handlers one at a time, so a handler that waited
    # on an fsync would hold up the handler that clears the floor.
    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    database.stalling = True

    await service.on_shot_completed(ShotCompleted(shot=make_shot()))

    assert service.shots_offered == 1
    assert service.shots_recorded == 0, "the handler waited for the write"

    await database.release()
    await until(lambda: service.shots_recorded == 1)
    assert service.shots_recorded == 1
    await service.aclose()


async def test_a_question_does_not_queue_behind_a_shot_being_written(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    """The tier boundary, enforced. Writing shots is the durable tier and asking
    history questions is the enrichment tier, and one thread for both means an
    assistant asking in a loop puts its queries in front of shots waiting to be
    recorded — the enrichment tier delaying the durable one."""

    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 1)
    database.stalling = True
    # A write that will not finish, holding the writer's one thread.
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))

    answered = await asyncio.wait_for(service.last_shot(), timeout=2.0)

    assert answered is not None
    assert service.read_failures == 0
    await database.release()
    await service.aclose()


async def test_a_reader_that_cannot_be_opened_falls_back_to_the_writer(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    """A second connection is an optimisation, not a requirement. Losing it must
    cost the separation and nothing else."""

    class NoReader(Database):
        def reader(self) -> Database:
            broken = Database(self.path / "not-a-directory")
            return broken

    service = await service_for(NoReader(tmp_path / "history.sqlite3"))
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 1)

    assert service.is_available is True
    assert await service.last_shot() is not None
    await service.aclose()


async def test_the_reader_will_not_write_even_if_it_is_asked_to(
    tmp_path: Path,
) -> None:
    """`query_only` rather than trust. The point of the second connection is that
    one of them is the only one allowed to change the file, and a promise kept by
    convention is not kept."""

    writer = Database(tmp_path / "history.sqlite3")
    await writer.open()
    reader = writer.reader()
    await reader.open()
    try:
        with pytest.raises(Exception, match="readonly|read-only"):
            await reader.run(lambda connection: connection.execute("DELETE FROM shots"))
    finally:
        await reader.close()
        await writer.close()


async def test_a_stalled_database_does_not_stall_the_next_shot(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    database.stalling = True

    for _ in range(3):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))

    assert service.shots_offered == 3
    assert service.shots_dropped == 0
    await database.release()
    await service.aclose()


# --- when it goes wrong ---------------------------------------------------


async def test_a_database_that_will_not_open_disables_history(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # A directory where a file should be. Somebody about to hit a ball does not
    # care that last week's statistics are unavailable.
    service = build_history_service(enabled=True, path=tmp_path)

    await service.open()

    assert service.is_available is False
    assert service.health().last_error is not None
    # And it stays harmless afterwards.
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    assert service.shots_offered == 0
    await service.aclose()


async def test_a_write_that_fails_is_counted_and_survived(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    database = FailingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    database.failing = True

    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.write_failures == 1)

    assert service.write_failures == 1
    assert service.shots_recorded == 0
    assert "disk I/O error" in (service.health().last_error or "")

    # And the next shot is still attempted rather than the writer having died.
    database.failing = False
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 1)
    assert service.shots_recorded == 1
    await service.aclose()


async def test_a_read_that_fails_answers_with_nothing_rather_than_raising(
    tmp_path: Path,
) -> None:
    # A statistics view is not worth an exception on a machine mid-round.
    database = FailingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database)
    database.failing = True

    assert await service.sessions() == []
    assert await service.shots() == []
    assert await service.analytics() is None

    database.failing = False
    await service.aclose()


async def test_a_full_queue_refuses_the_newest_shot_and_says_so(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # Throwing away a shot already accepted would be worse than declining one.
    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database, queue_size=1)
    database.stalling = True

    # The first is picked up by the writer, the second fills the queue, and the
    # third has nowhere to go.
    for _ in range(3):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))
        await asyncio.sleep(0)

    assert service.shots_dropped == 1
    await database.release()
    await until(lambda: service.shots_recorded == 2)
    assert service.shots_recorded == 2
    await service.aclose()


async def test_a_dropped_shot_leaves_a_gap_in_the_numbering(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # The gap is the record that something was lost. Renumbering would hide it.
    database = StallingDatabase(tmp_path / "history.sqlite3")
    service = await service_for(database, queue_size=1)
    database.stalling = True
    for _ in range(3):
        await service.on_shot_completed(ShotCompleted(shot=make_shot()))
        await asyncio.sleep(0)
    await database.release()
    await until(lambda: service.shots_recorded == 2)

    ordinals = [read.ordinal for read in await service.shots()]

    assert ordinals == [1, 2]
    assert service.shots_offered == 3
    await service.aclose()


async def test_history_switched_off_writes_no_file(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    service = build_history_service(enabled=False, path=path)

    await service.open()
    await service.aclose()

    assert not path.exists()
    assert service.is_available is False


async def test_a_shot_offered_to_disabled_history_is_not_an_error(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    service = build_history_service(enabled=False, path=tmp_path / "history.sqlite3")
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=make_shot()))

    assert service.shots_offered == 0
    await service.aclose()


# --- raw payloads ---------------------------------------------------------


async def test_a_shot_carrying_a_raw_payload_stores_it(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = make_shot(raw_payload='{"ShotNumber": 1, "BallData": {}}')

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await until(lambda: service.shots_recorded == 1)

    payload = await service.payload(shot.shot_id)
    assert payload is not None
    assert payload.payload == '{"ShotNumber": 1, "BallData": {}}'
    assert payload.source_kind == "gspro_connect"


async def test_a_shot_with_no_raw_payload_stores_none(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = make_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await until(lambda: service.shots_recorded == 1)

    assert await service.payload(shot.shot_id) is None


# --- reading back --------------------------------------------------------


async def test_the_service_summarises_the_session_in_progress(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    for speed in (148.0, 152.0):
        await service.on_shot_completed(
            ShotCompleted(shot=make_shot(ball=launch(speed=speed)))
        )
    await service.on_shot_completed(
        ShotCompleted(shot=make_shot(club=ClubKind.PUTTER, ball=launch(speed=6.0)))
    )
    await until(lambda: service.shots_recorded == 3)

    summary = await service.analytics()

    assert summary is not None
    assert summary.shots == 3
    assert summary.overall["ball_speed"].maximum == 152.0
    assert {aggregate.club for aggregate in summary.by_club} == {
        ClubKind.DRIVER,
        ClubKind.PUTTER,
    }


async def test_summarising_a_session_that_does_not_exist_gives_nothing(
    service: HistoryService,
) -> None:
    assert await service.analytics(uuid4()) is None


async def test_an_analysis_can_be_attached_through_the_service(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    shot = make_shot()
    await service.on_shot_completed(ShotCompleted(shot=shot))
    await until(lambda: service.shots_recorded == 1)

    attached = await service.attach_analysis(shot.shot_id, "swing_video", "clip.mp4")

    assert attached is not None
    assert await service.analyses(shot.shot_id) == [attached]


# --- wired to the bus ----------------------------------------------------


async def test_shots_published_on_the_bus_are_recorded(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    bus = AsyncEventBus()
    service.subscribe(bus)
    await bus.start()
    try:
        await bus.publish(ShotCompleted(shot=make_shot()))
        await bus.drain(timeout=1.0)
        await until(lambda: service.shots_recorded == 1)
    finally:
        await bus.stop()

    assert service.shots_recorded == 1


async def test_health_reports_what_history_is_doing(
    service: HistoryService, make_shot: ShotBuilder
) -> None:
    await service.on_shot_completed(ShotCompleted(shot=make_shot()))
    await until(lambda: service.shots_recorded == 1)

    health = service.health()

    assert health.enabled is True
    assert health.available is True
    assert health.shots_offered == 1
    assert health.shots_recorded == 1
    assert health.shots_dropped == 0
    assert health.schema_version >= 1
    assert health.session_id is not None
    assert health.last_error is None
