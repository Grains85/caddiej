"""Passive observer of GSPro currentRound.dat.

Read-only. Polls on a timer and publishes only ShotIDs that appeared after
observation began. Does not listen on Open Connect and does not modify GSPro.

Priming happens on the first successful parse and nowhere else. The file
outlives both GSPro and CaddieJ, so an absent file is not evidence of a fresh
round — it may be a path that is briefly unreadable, or one that becomes valid
later and still holds last week's play. Treating "missing" as "primed" meant
every historical shot in it was published the moment it appeared: a floor
result, a history row and a model call each, for balls struck days ago.

A shot that cannot be published is not marked seen. The file still has it, and
the next poll tries again — which is why an unchanged file is still read when
a retry is outstanding.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from caddiej.adapters.gspro.round_normalize import shot_from_round
from caddiej.adapters.gspro.round_parse import parse_round_document
from caddiej.core.events import Event
from caddiej.domain.events import RoundShotObserved
from caddiej.domain.observation import ObservedRoundShot

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]

# Consecutive failed reads before the observer stops calling itself healthy.
# GSPro holds the file open while it writes, so a single failure is ordinary;
# a run of them is a lock that is not going to clear, or a bad path.
_DEGRADED_AFTER_FAILURES = 5

# How many shot ids to remember. A round is eighteen holes of a handful of
# shots each; this is two orders of magnitude above that, so forgetting can
# only ever reach ids GSPro itself dropped from the document long ago.
_SEEN_LIMIT = 4096


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
    last_read_at: str | None
    read_failures: int
    shots_deferred: int


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
        self._monotonic = monotonic or time.monotonic
        # One id for this run of this observer, so every shot read from the file
        # in one sitting is attributed to the same reading of it. Not GSPro's
        # round: the file outlives the process, and a device session is what
        # CaddieJ was watching rather than what the simulator was doing.
        self._session_id = uuid4()
        # Bounded, and in insertion order. Every shot GSPro has ever written to
        # this file would otherwise stay here for as long as the process runs,
        # and this is the one part of CaddieJ meant to be left watching all
        # day. The limit is far above any round: the ids that matter are the
        # ones still in the document, and GSPro starts a new round by
        # replacing it.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._primed = False
        self._parse_failures = 0
        # Whether the newest thing GSPro wrote could be read as a document.
        self._unparsed = False
        self._shots_observed = 0
        self._last_shot_id: str | None = None
        self._last_change_at: datetime | None = None
        self._seen_file = False
        self._last_size: int | None = None
        self._last_mtime_ns: int | None = None
        self._read_failures = 0
        self._last_read_at: datetime | None = None
        # A shot the bus refused. Set so the next poll reads an otherwise
        # unchanged file rather than short-circuiting past a shot it still owes.
        self._retry_pending = False
        self.shots_deferred = 0

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
            last_read_at=(
                None if self._last_read_at is None else self._last_read_at.isoformat()
            ),
            read_failures=self._read_failures,
            shots_deferred=self.shots_deferred,
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
            # `_primed` is deliberately not touched. See the module docstring:
            # this file persists between GSPro runs, so one that appears later
            # is not necessarily a new round.
            return ()
        except OSError:
            self._note_read_failure()
            return ()

        self._seen_file = True
        self._read_failures = 0
        self._last_read_at = self._now()
        unchanged = (
            stat.st_size == self._last_size and stat.st_mtime_ns == self._last_mtime_ns
        )
        if unchanged and self._primed and not self._retry_pending:
            return ()

        parsed = parse_round_document(text)
        if parsed is None:
            # The size and mtime are deliberately not recorded here. `stat` and
            # the read are two calls, so GSPro can finish writing between them
            # and hand back a torn buffer under metadata that is already
            # final. Recording it would make the next poll call the file
            # unchanged and skip the read entirely — permanently blind to a
            # document that is sitting there complete, until the shot after
            # this one happens to change the size again.
            self._parse_failures += 1
            self._unparsed = True
            logger.debug(
                "gspro_round_parse_failed",
                extra={
                    "gspro_round_file": str(self._path),
                    "gspro_parse_failures": self._parse_failures,
                },
            )
            return ()

        self._unparsed = False
        self._last_size = stat.st_size
        self._last_mtime_ns = stat.st_mtime_ns

        changed_at = self._now()
        self._last_change_at = changed_at
        if not self._primed:
            for shot in parsed:
                self._remember(shot.shot_id)
            self._primed = True
            logger.info(
                "gspro_round_observer_primed",
                extra={
                    "gspro_round_file": str(self._path),
                    "observer_status": "watching",
                },
            )
            return ()

        self._retry_pending = False
        emitted: list[ObservedRoundShot] = []
        for shot in parsed:
            if shot.shot_id in self._seen:
                continue
            measured = shot_from_round(
                shot,
                self._session_id,
                changed_at,
                self._monotonic(),
            )
            event = RoundShotObserved(
                shot=shot, measured=measured, source="gspro_round_file"
            )
            try:
                await self._publish(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Left out of `_seen` on purpose, so the next poll offers it
                # again. Stopping rather than skipping ahead keeps the shots
                # in the order they were struck: whatever refused this one is
                # about to refuse the next, and a shot that arrives a tenth of
                # a second late is better than one that never arrives.
                self._retry_pending = True
                self.shots_deferred += 1
                logger.warning(
                    "gspro_round_shot_deferred",
                    extra={
                        "gspro_shot_id": shot.shot_id,
                        "gspro_shots_deferred": self.shots_deferred,
                    },
                    exc_info=True,
                )
                break

            self._remember(shot.shot_id)
            self._last_shot_id = shot.shot_id
            self._shots_observed += 1
            emitted.append(shot)
            extra = {
                "gspro_shot_id": shot.shot_id,
                # Which of the two paths this shot can take. A round entry
                # with no ball speed reaches the floor and stops there, and
                # a session where that is every shot should say so in one
                # field rather than through an absence of diagnoses.
                "shot_id": None if measured is None else str(measured.shot_id),
            }
            if measured is None:
                extra["coaching_reason"] = "no_measurement"
                extra["coaching_detail"] = (
                    "the round entry had no verified ball speed; "
                    "ShotCompleted was not published and coaching was not started"
                )
            logger.info("gspro_round_shot_observed", extra=extra)
        return tuple(emitted)

    def _remember(self, shot_id: str) -> None:
        """Record a shot id as seen, forgetting the oldest once full."""

        self._seen[shot_id] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)

    def _note_read_failure(self) -> None:
        self._read_failures += 1
        if self._read_failures == _DEGRADED_AFTER_FAILURES:
            logger.warning(
                "gspro_round_file_unreadable",
                extra={
                    "gspro_round_file": str(self._path),
                    "observer_status": "degraded",
                    "observer_read_failures": self._read_failures,
                },
                exc_info=True,
            )
        else:
            logger.debug("gspro_round_file_unreadable", exc_info=True)

    def _status(self) -> RoundObserverStatus:
        if self._path is None:
            return RoundObserverStatus.WAITING
        if self._read_failures >= _DEGRADED_AFTER_FAILURES:
            # A file GSPro is holding open, a path that has stopped
            # resolving: reporting `watching` through either is how an
            # observer that has read nothing since seven o'clock looks
            # exactly like a quiet evening.
            return RoundObserverStatus.DEGRADED
        if not self._seen_file:
            return RoundObserverStatus.WAITING
        if self._parse_failures > 0 and not self._primed:
            return RoundObserverStatus.DEGRADED
        if self._unparsed:
            # The newest thing GSPro wrote could not be read as a document.
            # Reported however long the session has been running, because a
            # round file that stopped parsing after the fourth hole is an
            # observer that can see the file and understand none of it, which
            # `watching` describes as a quiet evening.
            return RoundObserverStatus.DEGRADED
        return RoundObserverStatus.WATCHING
