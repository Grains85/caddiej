"""Recording shots, without ever making a player wait for a disk.

Two rules shape everything here.

**Nothing on the event path touches the database.** `on_shot_completed` puts the
shot on a queue and returns; it performs no awaits at all. A separate task writes.
The bus isolates subscribers, so a handler that waited on an fsync would no
longer hold up the floor — but this handler still must not, because history's
own job is to get off the event path.

**History is never a reason to stop playing golf.** A database that will not open,
a disk that fills, a file locked by something else: all of them are reported and
survived. The one thing that must never happen is a lost shot being silently
lost, so every drop is counted, logged, and visible in `health()`.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import UUID, uuid4

from caddiej.core.event_bus import EventBus
from caddiej.domain.coaching import AdviceOutcome
from caddiej.domain.events import ShotCompleted
from caddiej.persistence.analytics import SessionAnalytics, summarise_session
from caddiej.persistence.database import Database
from caddiej.persistence.records import (
    AnalysisReference,
    RawPayload,
    SessionRecord,
    StoredAdvice,
    StoredShot,
)
from caddiej.persistence.repository import HistoryRepository, insert_shots

logger = logging.getLogger(__name__)

Now = Callable[[], datetime]

_STOP = object()
# One commit can absorb a backlog, and catching up is the only time a backlog
# exists. Bounded so that a pathological queue does not become one giant write.
_MAX_BATCH = 64
# How many recently written shots to remember, so advice can tell whether the
# row it references has landed. Only the last few seconds are ever asked about.
_WRITTEN_LIMIT = 512


@dataclass(frozen=True, slots=True)
class HistoryHealth:
    """Everything that can go wrong with history, in one place."""

    enabled: bool
    available: bool
    path: str | None
    schema_version: int
    session_id: str | None
    shots_offered: int
    shots_recorded: int
    shots_queued: int
    shots_dropped: int
    write_failures: int
    read_failures: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class _PendingWrite:
    stored: StoredShot
    payload: RawPayload | None


def app_version() -> str:
    """The CaddieJ that wrote a row, for reading a database a year later."""

    try:
        return version("caddiej")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "unknown"


class HistoryService:
    """Owns the history database, the session, and the writer behind it."""

    def __init__(
        self,
        database: Database,
        enabled: bool = True,
        player_name: str | None = None,
        label: str | None = None,
        queue_size: int = 256,
        drain_timeout_seconds: float = 5.0,
        now: Now = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        # Opened alongside the writer, and only for questions. See
        # `Database.reader`: without it, anything that asks history a question
        # queues its query in front of shots waiting to be written.
        self._reader: Database | None = None
        self._enabled = enabled
        self._player_name = player_name
        self._label = label
        self._drain_timeout = drain_timeout_seconds
        self._now = now

        self._queue: asyncio.Queue[_PendingWrite | object] = asyncio.Queue(
            maxsize=queue_size
        )
        self._writer: asyncio.Task[None] | None = None
        self._available = False
        self._session: SessionRecord | None = None
        self._session_stored = False
        # Which shots are actually on disk, and a pulse each time that changes.
        # Advice references a shot row, so it has to be able to wait for one.
        self._written: OrderedDict[UUID, None] = OrderedDict()
        self._progress = asyncio.Event()

        self.shots_offered = 0
        self.shots_recorded = 0
        self.shots_dropped = 0
        self.write_failures = 0
        # Counted, not just logged, because a caller has no other way to tell a
        # query that failed from a query that found nothing: reads fail soft and
        # both answer with the default. Anything that must not report an error as
        # an absence — the assistant boundary does — compares this across a call.
        self.read_failures = 0
        self.last_error: str | None = None

    # --- lifecycle --------------------------------------------------------

    @property
    def session_id(self) -> UUID | None:
        return None if self._session is None else self._session.session_id

    @property
    def is_available(self) -> bool:
        return self._available

    async def open(self) -> None:
        """Open the database and prepare a session. Never raises.

        A failure here disables history for the rest of the run rather than
        stopping CaddieJ: somebody about to hit a ball does not care that last
        week's statistics are unavailable.

        The session is only prepared, not written. Its row appears with the
        first shot, so a run that recorded nothing leaves nothing behind and
        opening the file to read it does not add to it.
        """

        if not self._enabled:
            logger.info("history_disabled")
            return

        try:
            await self._database.open()
        except Exception as error:
            self._fail("history_unavailable", error)
            await self._database.close()
            return

        # After the writer, because the file and its schema have to exist first.
        # Failing is survivable: queries fall back to the writer's connection,
        # which is how this worked before and is only ever slower.
        reader = self._database.reader()
        try:
            await reader.open()
        except Exception as error:
            self._fail("history_reader_unavailable", error)
            await reader.close()
        else:
            self._reader = reader

        session = SessionRecord(
            session_id=uuid4(),
            started_at=self._now(),
            label=self._label,
            app_version=app_version(),
        )
        self._session = session
        self._available = True
        self._writer = asyncio.create_task(self._write_forever(), name="history-writer")
        logger.info(
            "history_session_started",
            extra={
                "history_session": str(session.session_id),
                "history_path": str(self._database.path),
            },
        )

    async def aclose(self) -> None:
        """Finish the queue, close the session, and let go of the file."""

        if self._writer is not None:
            await self._queue.put(_STOP)
            try:
                await asyncio.wait_for(self._writer, self._drain_timeout)
            except TimeoutError:
                # Whatever is left is lost, but shutdown is not negotiable.
                logger.warning(
                    "history_drain_timed_out",
                    extra={"history_queued": self._queue.qsize()},
                )
                self._writer.cancel()
                await asyncio.gather(self._writer, return_exceptions=True)
            self._writer = None

        # Nothing to stamp if no shot ever created the row.
        if self._available and self._session_stored and self._session is not None:
            await self._close_session(self._session)

        self._available = False
        reader = self._reader
        self._reader = None
        if reader is not None:
            await reader.close()
        await self._database.close()

    async def _close_session(self, session: SessionRecord) -> None:
        try:
            ended_at = self._now()
            recorded = self.shots_recorded
            await self._database.run(
                lambda connection: HistoryRepository(connection).close_session(
                    session.session_id, ended_at, recorded
                )
            )
            self._session = replace(session, ended_at=ended_at, shot_count=recorded)
        except Exception as error:
            # The session row keeps a null `ended_at`, which reads correctly as
            # "this run did not finish cleanly".
            self._fail("history_session_close_failed", error)

    # --- the event path ---------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(ShotCompleted, self.on_shot_completed, name="history")

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Take a shot for storage. Awaits nothing, so it can never block."""

        if not self._available or self._session is None:
            return

        self.shots_offered += 1
        stored = StoredShot(
            shot=event.shot,
            session_id=self._session.session_id,
            # Assigned on arrival, not on success, so a dropped shot leaves a
            # gap. A gap in the ordinals is a record that something was lost.
            ordinal=self.shots_offered,
            recorded_at=self._now(),
            player_name=self._player_name,
        )
        self._offer(_PendingWrite(stored=stored, payload=self._payload_of(stored)))

    def _payload_of(self, stored: StoredShot) -> RawPayload | None:
        raw = stored.shot.source.raw_payload
        if raw is None:
            return None
        return RawPayload(
            shot_id=stored.shot.shot_id,
            captured_at=stored.recorded_at,
            source_kind=stored.shot.source.kind,
            payload=raw,
        )

    def _offer(self, write: _PendingWrite) -> None:
        try:
            self._queue.put_nowait(write)
        except asyncio.QueueFull:
            # The newest is refused rather than making room by discarding a shot
            # already accepted: throwing away work we took responsibility for is
            # worse than declining more. Getting here means the writer is stuck,
            # so it is an error however it is resolved.
            self.shots_dropped += 1
            logger.error(
                "history_shot_dropped",
                extra={
                    "history_dropped": self.shots_dropped,
                    "shot_id": str(write.stored.shot.shot_id),
                },
            )

    # --- the writer -------------------------------------------------------

    async def _write_forever(self) -> None:
        """Drain the queue until told to stop. Must not raise."""

        try:
            stopping = False
            while not stopping:
                first = await self._queue.get()
                batch: list[_PendingWrite] = []
                for item in (first, *self._available_now()):
                    if item is _STOP:
                        stopping = True
                    elif isinstance(item, _PendingWrite):
                        batch.append(item)
                if batch:
                    await self._write(batch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A bug in the writer must not take the process with it. History
            # stops here and says so; the queue is drained so callers do not
            # block on a queue nobody is reading.
            self._fail("history_writer_stopped", error)
            self._available = False

    def _available_now(self) -> list[_PendingWrite | object]:
        items: list[_PendingWrite | object] = []
        while len(items) < _MAX_BATCH - 1:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def _write(self, batch: Sequence[_PendingWrite]) -> None:
        rows = [(write.stored, write.payload) for write in batch]
        # The first batch carries the session with it, in the same transaction:
        # a shot belonging to a session row that failed to appear would violate
        # the foreign key and lose the shot too.
        session = None if self._session_stored else self._session
        try:
            await self._database.run(
                lambda connection: insert_shots(connection, rows, session)
            )
        except Exception as error:
            # These shots are gone. Reported per batch rather than per shot so a
            # failing disk cannot itself flood the log.
            self.write_failures += 1
            self._fail("history_write_failed", error, count=len(batch))
            return

        self._session_stored = True
        self.shots_recorded += len(batch)
        for write in batch:
            self._written[write.stored.shot.shot_id] = None
        while len(self._written) > _WRITTEN_LIMIT:
            self._written.popitem(last=False)
        self._progress.set()
        self._progress.clear()
        logger.info(
            "history_shots_recorded",
            extra={
                "history_recorded": self.shots_recorded,
                "history_batch": len(batch),
            },
        )

    def _fail(self, event: str, error: BaseException, count: int | None = None) -> None:
        self.last_error = f"{type(error).__name__}: {error}"
        extra: dict[str, object] = {"history_path": str(self._database.path)}
        if count is not None:
            extra["history_batch"] = count
        logger.error(event, extra=extra, exc_info=error)

    # --- reading ----------------------------------------------------------

    async def sessions(self, limit: int = 50) -> list[SessionRecord]:
        return await self._read(
            lambda repository: repository.sessions(limit), default=[]
        )

    async def shots(self, session_id: UUID | None = None) -> list[StoredShot]:
        target = self._resolve(session_id)
        if target is None:
            return []
        return await self._read(lambda repository: repository.shots(target), default=[])

    async def last_shot(self, session_id: UUID | None = None) -> StoredShot | None:
        """The most recent shot of a session, defaulting to the one in progress.

        Answers None both for "nobody has hit anything" and for "history is not
        working", which callers that need to tell those apart resolve with
        `is_available`. Every read here fails soft the same way.
        """

        target = self._resolve(session_id)
        if target is None:
            return None
        return await self._read(
            lambda repository: repository.last_shot(target), default=None
        )

    async def analytics(
        self, session_id: UUID | None = None
    ) -> SessionAnalytics | None:
        """Summarise a session, defaulting to the one in progress.

        Both reads happen in one trip to the database thread, so the summary
        cannot describe a session whose shots were counted a moment later.
        """

        target = self._resolve(session_id)
        if target is None:
            return None

        def read(repository: HistoryRepository) -> SessionAnalytics | None:
            session = repository.session(target)
            if session is None:
                return None
            return summarise_session(session, repository.shots(target))

        return await self._read(read, default=None)

    async def payload(self, shot_id: UUID) -> RawPayload | None:
        return await self._read(
            lambda repository: repository.payload(shot_id), default=None
        )

    async def attach_analysis(
        self, shot_id: UUID, kind: str, reference: str
    ) -> AnalysisReference | None:
        """Point at something produced about a shot by another subsystem."""

        record = AnalysisReference(
            analysis_id=uuid4(),
            shot_id=shot_id,
            kind=kind,
            reference=reference,
            created_at=self._now(),
        )

        def write(repository: HistoryRepository) -> AnalysisReference:
            repository.attach_analysis(record)
            return record

        return await self._read(write, default=None, writes=True)

    async def analyses(self, shot_id: UUID) -> list[AnalysisReference]:
        return await self._read(
            lambda repository: repository.analyses(shot_id), default=[]
        )

    # --- coaching ---------------------------------------------------------

    async def record_advice(self, outcome: AdviceOutcome) -> StoredAdvice | None:
        """Store what was concluded about a shot. Never raises.

        Written directly rather than through the shot queue, because the caller
        is already a background worker whose whole purpose is to do slow things
        off the event path, so a second queue would buy nothing.

        It does have to wait for the shot itself, though. Shots are written by a
        batching worker on another thread and advice references one by foreign
        key, so an answer that arrives quickly — a cached line, a fast model, a
        diagnosis with nothing to ask about — really can overtake the shot it is
        about and be rejected. Waiting costs nothing in the normal case and the
        line is already on the floor either way.

        Answers None on failure, like every other write here. A coaching line
        the player already read is not worth an exception because the row
        describing it did not land.
        """

        await self._await_shot(outcome.advice.shot_id)
        record = StoredAdvice(advice_id=uuid4(), outcome=outcome)

        def write(repository: HistoryRepository) -> StoredAdvice:
            repository.insert_advice(record)
            return record

        return await self._read(write, default=None, writes=True)

    async def _await_shot(self, shot_id: UUID) -> None:
        """Wait, briefly, for a shot to reach the disk.

        Gives up rather than waiting forever: the shot may have been dropped, or
        its batch may have failed, and neither is a reason to hold a coaching
        worker open. The insert is then attempted anyway, so the foreign key
        refusing it is logged as what it is.
        """

        if not self._available:
            return
        try:
            async with asyncio.timeout(self._drain_timeout):
                while shot_id not in self._written:
                    await self._progress.wait()
        except TimeoutError:
            logger.warning(
                "history_advice_waited_for_a_shot_that_never_arrived",
                extra={"shot_id": str(shot_id)},
            )

    async def advice(self, shot_id: UUID) -> list[StoredAdvice]:
        return await self._read(
            lambda repository: repository.advice(shot_id), default=[]
        )

    async def session_advice(
        self, session_id: UUID | None = None
    ) -> list[StoredAdvice]:
        target = self._resolve(session_id)
        if target is None:
            return []
        return await self._read(
            lambda repository: repository.session_advice(target), default=[]
        )

    async def recent_advice(
        self, limit: int, session_id: UUID | None = None
    ) -> list[StoredAdvice]:
        """The last `limit` coaching lines of a session, oldest of them first."""

        target = self._resolve(session_id)
        if target is None or limit <= 0:
            return []
        return await self._read(
            lambda repository: repository.recent_advice(target, limit), default=[]
        )

    async def focus_counts(self, session_id: UUID | None = None) -> dict[str, int]:
        """What an evening was actually full of, by fault."""

        target = self._resolve(session_id)
        if target is None:
            return {}
        return await self._read(
            lambda repository: repository.focus_counts(target), default={}
        )

    def _resolve(self, session_id: UUID | None) -> UUID | None:
        if session_id is not None:
            return session_id
        return self.session_id

    async def _read[ResultType](
        self,
        work: Callable[[HistoryRepository], ResultType],
        default: ResultType,
        writes: bool = False,
    ) -> ResultType:
        """Run a query, answering with `default` if history is not working.

        Reads fail soft for the same reason writes do: a statistics view is not
        worth an exception on a machine mid-round.

        Questions go to the reader's own connection so that asking them cannot
        delay a shot being written; the two callers that write go to the writer's,
        because that is the connection allowed to.
        """

        if not self._available:
            return default
        database = self._database
        if not writes and self._reader is not None:
            database = self._reader
        try:
            return await database.run(
                lambda connection: work(HistoryRepository(connection))
            )
        except Exception as error:
            self.read_failures += 1
            self._fail("history_read_failed", error)
            return default

    # --- reporting --------------------------------------------------------

    def health(self) -> HistoryHealth:
        return HistoryHealth(
            enabled=self._enabled,
            available=self._available,
            path=str(self._database.path) if self._enabled else None,
            schema_version=self._database.schema_version,
            session_id=None if self.session_id is None else str(self.session_id),
            shots_offered=self.shots_offered,
            shots_recorded=self.shots_recorded,
            shots_queued=self._queue.qsize(),
            shots_dropped=self.shots_dropped,
            write_failures=self.write_failures,
            read_failures=self.read_failures,
            last_error=self.last_error,
        )


def build_history_service(
    enabled: bool,
    path: Path | str,
    player_name: str | None = None,
    label: str | None = None,
    queue_size: int = 256,
    busy_timeout_seconds: float = 5.0,
    drain_timeout_seconds: float = 5.0,
) -> HistoryService:
    """Assemble the service from plain values, so tests need no `Settings`."""

    return HistoryService(
        database=Database(path, busy_timeout_seconds=busy_timeout_seconds),
        enabled=enabled,
        player_name=player_name or None,
        label=label or None,
        queue_size=queue_size,
        drain_timeout_seconds=drain_timeout_seconds,
    )
