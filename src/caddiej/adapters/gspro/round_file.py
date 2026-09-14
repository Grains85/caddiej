"""Passive observer of GSPro currentRound.dat.

Read-only. Polls on a timer and publishes only ShotIDs that appeared after
observation began. Does not listen on Open Connect and does not modify GSPro.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from caddiej.adapters.gspro.round_parse import parse_round_document
from caddiej.core.events import Event
from caddiej.domain.events import RoundShotObserved
from caddiej.domain.observation import ObservedRoundShot

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]


class RoundObserverStatus(StrEnum):
    DISABLED = "disabled"
    WAITING = "waiting"
    WATCHING = "watching"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class GSProRoundObserverHealth:
    status: RoundObserverStatus
    file: str | None
    last_change_at: str | None
    last_shot_id: str | None
    shots_observed: int
    parse_failures: int


class GSProRoundFileAdapter:
    """Watch currentRound.dat and emit unseen round-play shots."""

    def __init__(
        self,
        path: Path | None,
        publish: Publish,
        *,
        poll_interval: float = 0.1,
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._path = path
        self._publish = publish
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._seen: set[str] = set()
        self._primed = False
        self._parse_failures = 0
        self._shots_observed = 0
        self._last_shot_id: str | None = None
        self._last_change_at: datetime | None = None
        self._seen_file = False
        self._last_size: int | None = None
        self._last_mtime_ns: int | None = None

    def health(self) -> GSProRoundObserverHealth:
        return GSProRoundObserverHealth(
            status=self._status(),
            file=None if self._path is None else str(self._path),
            last_change_at=(
                None
                if self._last_change_at is None
                else self._last_change_at.isoformat()
            ),
            last_shot_id=self._last_shot_id,
            shots_observed=self._shots_observed,
            parse_failures=self._parse_failures,
        )

    async def serve(self) -> None:
        if self._path is None:
            logger.info(
                "gspro_round_observer_waiting",
                extra={"gspro_round_file": "", "observer_status": "waiting"},
            )
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gspro_round_observer_poll_failed")
            await self._sleep(self._poll_interval)

    async def poll(self) -> tuple[ObservedRoundShot, ...]:
        """One poll. Exposed so tests can drive the adapter without sleeping."""

        if self._path is None:
            return ()
        try:
            stat = await asyncio.to_thread(self._path.stat)
            text = await asyncio.to_thread(
                self._path.read_text, encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            self._seen_file = False
            self._last_size = None
            self._last_mtime_ns = None
            # No file means no historical shots. A round that starts later is live.
            self._primed = True
            return ()
        except OSError:
            logger.debug("gspro_round_file_unreadable", exc_info=True)
            return ()

        self._seen_file = True
        unchanged = (
            stat.st_size == self._last_size and stat.st_mtime_ns == self._last_mtime_ns
        )
        self._last_size = stat.st_size
        self._last_mtime_ns = stat.st_mtime_ns
        if unchanged and self._primed:
            return ()

        parsed = parse_round_document(text)
        if parsed is None:
            self._parse_failures += 1
            logger.debug(
                "gspro_round_parse_failed",
                extra={
                    "gspro_round_file": str(self._path),
                    "gspro_parse_failures": self._parse_failures,
                },
            )
            return ()

        self._last_change_at = self._now()
        if not self._primed:
            self._seen.update(shot.shot_id for shot in parsed)
            self._primed = True
            logger.info(
                "gspro_round_observer_primed",
                extra={
                    "gspro_round_file": str(self._path),
                    "observer_status": "watching",
                },
            )
            return ()

        emitted: list[ObservedRoundShot] = []
        for shot in parsed:
            if shot.shot_id in self._seen:
                continue
            self._seen.add(shot.shot_id)
            self._last_shot_id = shot.shot_id
            self._shots_observed += 1
            emitted.append(shot)
            await self._publish(RoundShotObserved(shot=shot, source="gspro_round_file"))
            logger.info(
                "gspro_round_shot_observed",
                extra={"gspro_shot_id": shot.shot_id},
            )
        return tuple(emitted)

    def _status(self) -> RoundObserverStatus:
        if self._path is None:
            return RoundObserverStatus.WAITING
        if not self._seen_file:
            return RoundObserverStatus.WAITING
        if self._parse_failures > 0 and not self._primed:
            return RoundObserverStatus.DEGRADED
        return RoundObserverStatus.WATCHING
