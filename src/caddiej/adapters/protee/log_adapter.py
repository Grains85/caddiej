"""Passive observer of the ProTee United semantic main log.

Read-only tail of Logs/LogFile-YYYY-MM-DD.txt under the ProTee root.
Historical lines present when the adapter first sees a file are skipped; only
newly appended complete lines become events. A log that appears after having
been missing is live, because there was no history to skip.
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
        self._seen_file = False
        self._last_size = 0

    def health(self) -> ProTeeObserverHealth:
        return ProTeeObserverHealth(
            status=self._status(),
            current_log=None if self._path is None else str(self._path),
            last_event_at=(
                None if self._last_event_at is None else self._last_event_at.isoformat()
            ),
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

        rotated = self._path is not None and path != self._path
        if path != self._path:
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
            if stat.st_size < self._last_size:
                self._reset_read_state()
                self._skip_existing = False
            chunk = await asyncio.to_thread(_read_from, path, self._offset)
        except FileNotFoundError:
            self._seen_file = False
            self._reset_read_state()
            self._skip_existing = False
            return ()
        except OSError:
            logger.debug("protee_log_unreadable", exc_info=True)
            return ()

        self._seen_file = True
        self._last_size = stat.st_size
        self._offset += len(chunk)
        self._buffer += chunk
        lines, self._buffer = _split_complete_lines(self._buffer)
        if self._skip_existing:
            self._skip_existing = False
            logger.info(
                "protee_observer_primed",
                extra={"protee_log": str(path), "observer_status": "watching"},
            )
            return ()

        emitted: list[Event] = []
        for line in lines:
            text = line.decode("utf-8", errors="replace")
            for event in events_from_line(text):
                emitted.append(event)
                self._last_event_at = self._now()
                await self._publish(event)
        return tuple(emitted)

    def _reset_read_state(self) -> None:
        self._offset = 0
        self._buffer = b""
        self._last_size = 0

    def _status(self) -> ProTeeObserverStatus:
        if self._root is None:
            return ProTeeObserverStatus.WAITING
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
