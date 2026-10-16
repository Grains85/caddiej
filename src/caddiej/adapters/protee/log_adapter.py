"""Passive observer of the ProTee United semantic main log.

Read-only tail of Logs/LogFile-YYYY-MM-DD.txt under the ProTee root.
Historical lines present when the adapter first sees a file are skipped; only
newly appended complete lines become events. A log that appears after having
been missing is live, because there was no history to skip — but only when
nothing has been read from it yet. A file that blinks out and comes back is
the same file, resumed at the same offset.

Two rules keep this observer from being a way for CaddieJ to lose facts.

A refusal costs one event, not the rest of the chunk. The lines in a read are
consumed whether or not they were published, so a `publish` that raised used to
take every event after it in the same chunk — a strike, a club change and a
ball detection lost to one transient refusal, with the offset already moved
past them. Each event is now published on its own and a failure is counted.

Nothing here writes to the log, opens it for anything but reading, or requires
ProTee to know CaddieJ exists. ProTee talks to GSPro whether this is running or
not, and that is the point: this file can fail in every way it has and the
round continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from caddiej.adapters.protee.log_parse import events_from_line
from caddiej.core.events import Event

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
Today = Callable[[], date]

# Consecutive failed reads before the observer stops calling itself healthy.
# More than one because a log NLog is appending to is momentarily unreadable
# on Windows and that is not worth reporting, and few enough that a genuinely
# broken path shows within a second of polling. The same figure as the round
# observer's, because the two are read side by side on one health page and
# differing thresholds would make them say different things about the same
# kind of trouble.
_DEGRADED_AFTER_FAILURES = 5


class ProTeeObserverStatus(StrEnum):
    DISABLED = "disabled"
    WAITING = "waiting"
    WATCHING = "watching"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ProTeeObserverHealth:
    status: ProTeeObserverStatus
    current_log: str | None
    last_event_at: str | None
    last_read_at: str | None
    read_failures: int
    events_dropped: int


class ProTeeLogAdapter:
    """Follow the semantic ProTee log without modifying it."""

    def __init__(
        self,
        root: Path | None,
        publish: Publish,
        *,
        poll_interval: float = 0.1,
        today: Today | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._root = root
        self._publish = publish
        self._poll_interval = poll_interval
        self._today = today or (lambda: datetime.now().date())
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._offset = 0
        self._buffer = b""
        self._path: Path | None = None
        self._skip_existing = True
        self._last_event_at: datetime | None = None
        self._last_read_at: datetime | None = None
        self._seen_file = False
        self._read_failures = 0
        # Whether the file went missing while something had been read from it.
        # Noted on the poll that could not find it and acted on by the poll
        # that can see the returned file's size.
        self._reopened = False
        self.events_dropped = 0

    def health(self) -> ProTeeObserverHealth:
        return ProTeeObserverHealth(
            status=self._status(),
            current_log=None if self._path is None else str(self._path),
            last_event_at=(
                None if self._last_event_at is None else self._last_event_at.isoformat()
            ),
            last_read_at=(
                None if self._last_read_at is None else self._last_read_at.isoformat()
            ),
            read_failures=self._read_failures,
            events_dropped=self.events_dropped,
        )

    def log_path_for(self, day: date) -> Path | None:
        if self._root is None:
            return None
        return self._root / "Logs" / f"LogFile-{day.isoformat()}.txt"

    async def serve(self) -> None:
        if self._root is None:
            logger.info(
                "protee_observer_waiting",
                extra={"protee_log": "", "observer_status": "waiting"},
            )
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("protee_observer_poll_failed")
            await self._sleep(self._poll_interval)

    async def poll(self) -> tuple[Event, ...]:
        """One poll. Exposed so tests can drive the adapter without sleeping."""

        path = self.log_path_for(self._today())
        if path is None:
            self._path = None
            return ()

        leftover: tuple[Event, ...] = ()
        rotated = self._path is not None and path != self._path
        if path != self._path:
            if rotated and self._path is not None:
                # Yesterday's file, once more, before it is let go of. The
                # name changes at midnight whether or not the last thing
                # written to the old one has been read, and a ball struck in
                # the poll interval before the date turned over is a line
                # appended to a file this is about to stop looking at. Once
                # per day, and only when something is actually there.
                leftover = await self._read_tail_of(self._path)
            self._path = path
            self._reset_read_state()
            if rotated:
                self._skip_existing = False
                logger.info(
                    "protee_log_rotated",
                    extra={"protee_log": str(path), "observer_status": "watching"},
                )

        try:
            stat = await asyncio.to_thread(path.stat)
            if stat.st_size < self._offset or self._reopened:
                # The offset only means anything for the file it was measured
                # against. A file shorter than what has been read from it, or
                # one that disappeared and came back, is not that file, so the
                # offset addresses nothing and the only safe read is from the
                # start. Warned rather than silent: it is ProTee's own log
                # being replaced under a running session, which is worth an
                # operator's attention even though golf continues.
                logger.warning(
                    "protee_log_truncated",
                    extra={
                        "protee_log": str(path),
                        "protee_log_offset": self._offset,
                        "protee_log_size": stat.st_size,
                    },
                )
                self._reset_read_state()
            chunk = await asyncio.to_thread(_read_from, path, self._offset)
        except FileNotFoundError:
            self._seen_file = False
            # Remembered rather than acted on. The file may be mid-rotation and
            # back within the poll interval, and the decision about the offset
            # belongs to the poll that can see the returned file's size.
            self._reopened = self._offset > 0
            return leftover
        except OSError:
            self._note_read_failure(path)
            return leftover

        self._seen_file = True
        self._read_failures = 0
        self._last_read_at = self._now()
        self._offset += len(chunk)
        self._buffer += chunk
        lines, self._buffer = _split_complete_lines(self._buffer)
        if self._skip_existing:
            self._skip_existing = False
            logger.info(
                "protee_observer_primed",
                extra={"protee_log": str(path), "observer_status": "watching"},
            )
            return leftover

        emitted: list[Event] = [*leftover]
        for line in lines:
            text = line.decode("utf-8", errors="replace")
            for event in events_from_line(text):
                if await self._emit(event):
                    emitted.append(event)
        return tuple(emitted)

    async def _read_tail_of(self, path: Path) -> tuple[Event, ...]:
        """Whatever was appended to a file after it was last read.

        Called on rotation only, against the previous day's log and while the
        offset and buffer still belong to it. Best effort by design: the file
        may already be gone or held, and a shot that cannot be recovered is
        worth less than a poll loop that stops.
        """

        if self._skip_existing:
            # Never read from, so everything in it is history, and history is
            # not what this observer publishes.
            return ()

        try:
            chunk = await asyncio.to_thread(_read_from, path, self._offset)
        except OSError:
            return ()

        lines, _ = _split_complete_lines(self._buffer + chunk)
        emitted: list[Event] = []
        for line in lines:
            for event in events_from_line(line.decode("utf-8", errors="replace")):
                if await self._emit(event):
                    emitted.append(event)
        if emitted:
            logger.info(
                "protee_log_tail_recovered",
                extra={
                    "protee_log": str(path),
                    "observer_events_recovered": len(emitted),
                },
            )
        return tuple(emitted)

    async def _emit(self, event: Event) -> bool:
        """Publish one event. A refusal costs that event and nothing after it."""

        try:
            await self._publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.events_dropped += 1
            logger.warning(
                "protee_event_dropped",
                extra={
                    "protee_event": type(event).__name__,
                    "observer_events_dropped": self.events_dropped,
                },
                exc_info=True,
            )
            return False
        self._last_event_at = self._now()
        return True

    def _note_read_failure(self, path: Path) -> None:
        """A read that failed for a reason other than the file being absent.

        Counted rather than logged at warning each time, because the thing
        worth an operator's attention is a log that keeps failing, and a
        single unreadable moment during a rotation is not that.
        """

        self._read_failures += 1
        logger.log(
            logging.WARNING
            if self._read_failures == _DEGRADED_AFTER_FAILURES
            else logging.DEBUG,
            "protee_log_unreadable",
            extra={
                "protee_log": str(path),
                "observer_read_failures": self._read_failures,
            },
            exc_info=True,
        )

    def _reset_read_state(self) -> None:
        self._offset = 0
        self._buffer = b""
        self._reopened = False

    def _status(self) -> ProTeeObserverStatus:
        if self._root is None:
            return ProTeeObserverStatus.WAITING
        if self._read_failures >= _DEGRADED_AFTER_FAILURES:
            # Reported even though the file was found, because a log that can
            # be seen and not read is an observer that will miss every shot
            # while still claiming to be watching for them.
            return ProTeeObserverStatus.DEGRADED
        if not self._seen_file:
            return ProTeeObserverStatus.WAITING
        return ProTeeObserverStatus.WATCHING


def _read_from(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read()


def _split_complete_lines(buffer: bytes) -> tuple[list[bytes], bytes]:
    if b"\n" not in buffer:
        return [], buffer
    *complete, remainder = buffer.split(b"\n")
    return complete, remainder
