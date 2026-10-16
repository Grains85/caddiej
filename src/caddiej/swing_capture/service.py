"""Reading ProTee's own swing replay back off the screen, through OBS.

ProTee Labs owns both swing cameras, records the swing, knows when impact was,
and draws two synchronised replays in its own window after every shot. CaddieJ
does not open those cameras, does not touch ProTee's encrypted recordings, and
does not click anything in ProTee. It waits for the replay to be on screen and
asks OBS for pictures of it.

Four rules shape this file, and three of them are the same rules the rest of
CaddieJ lives by.

**The event handler starts a task and returns.** A capture takes several
seconds by design. The bus worker that delivers `ShotDetected` also feeds the
floor's readiness and the ProTee observer behind it, so it may never wait here
— which is also why this subscriber's delivery policy is DROP.

**Newest shot wins, completely.** A second ball struck mid-capture cancels the
first capture where it stands. The generation counter is checked again inside
the lock that publishes, so a capture that was cancelled a moment too late
cannot overwrite the newer swing with an older one.

**OBS is never a reason golf stops, and never a cost either.** A missing
source, a WebSocket that is down, a request that hangs: each is logged and
abandoned. One camera failing does not discard the other one's stills, because
half a swing is worth looking at and nothing here is load-bearing. A source
name that does not exist is not a broken connection and does not close one,
and after three captures that produce nothing at all the subsystem stops
asking until the operator fixes it and restarts — the OBS instance being asked
is the one drawing the floor.

**Timing is configuration, not knowledge.** Nobody has measured how long ProTee
takes to put the replay on screen. The delay, the window and the rate are
therefore settings with a first guess for a default, and every capture logs
what it actually did so the guess can be corrected from a real shot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from caddiej.adapters.obs import CAPTURE_SCENE, ObsResourceNotFoundError
from caddiej.adapters.obs.screenshot import (
    UnusableScreenshot,
    require_usable_screenshot,
)
from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.cues import CapturePanel, PanelKind, PanelView
from caddiej.domain.events import (
    CoachingProgress,
    CoachingStage,
    ShotCompleted,
    ShotDetected,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCapture
from caddiej.domain.swing_phase import SwingPhase
from caddiej.swing_capture.cadence import (
    EARLY_SECONDS,
    Cadence,
    Segment,
    sample_times,
    spacing_floor,
    summarise,
)
from caddiej.swing_capture.phases import aim_capacity
from caddiej.swing_capture.select import (
    ActiveInterval,
    CapturedFrame,
    distinct_count,
    likely_blank,
    package_quota,
    select,
    settled_frame,
    static_choice,
)
from caddiej.swing_capture.store import (
    DIAGNOSTIC_DIRECTORY,
    EVIDENCE_KEEP,
    CameraResult,
    CaptureRecord,
    FrameTrace,
    LatestSwingStore,
    MetricsResult,
    Publication,
    retention_limit,
)
from caddiej.video.jpeg_size import jpeg_size

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]

# Enough of a bound that one unanswered screenshot cannot eat the whole window,
# and enough slack that a busy OBS still answers within it. Scaled to the
# capture rate rather than fixed: at 10 fps a still that takes half a second is
# already late; at 2 fps it is not.
_TIMEOUT_TICKS = 2.0
_MINIMUM_FRAME_TIMEOUT = 0.5

# After this many consecutive captures in which not one camera produced a
# still, stand down. The fault at that point is configuration or an OBS that
# is not answering, and a decoration subsystem retrying it forty times a shot
# is real work for the OBS instance that draws the floor.
_CAPTURES_BEFORE_GIVING_UP = 3

# How many strikes to sit out before looking again. A source name that is
# wrong at 20:00 is wrong at 23:00, so this is not a retry loop — but OBS
# still starting while CaddieJ already runs is the ordinary Windows sequence,
# and that resolves itself within a shot or two. Cheap enough at this spacing
# to be worth one attempt per handful of swings rather than none per session.
_SHOTS_BEFORE_TRYING_AGAIN = 10

# The All Values still is one request, not a burst, so it gets a longer bound
# than a 10 fps frame and one more look if the first is unusable or still shows
# the previous shot's grid.
_METRICS_TIMEOUT = 3.0
_METRICS_RETRY_SECONDS = 1.0

# The two body replays: the ones the timing diagnostic keeps whole, and the
# ones that are only ever aimed from a replay start.
_BODY_VIEWS = frozenset({PanelView.BEHIND, PanelView.SIDE})
_DIAGNOSTIC_VIEWS = _BODY_VIEWS

_NO_REPLAY_START = (
    "replay start not found on this body camera and none could be borrowed "
    "from the other; no stills kept rather than an unaimed spread that could "
    "include the loading screen"
)


class ScreenshotClient(Protocol):
    """The slice of `ObsClient` a swing capture uses. One request of it is new."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes: ...


ClientFactory = Callable[[], ScreenshotClient]


@dataclass(frozen=True, slots=True)
class SwingCaptureHealth:
    """Everything that can go wrong with swing capture, in one place."""

    enabled: bool
    sources: tuple[str, ...]
    panels: tuple[str, ...]
    directory: str | None
    captures_started: int
    captures_completed: int
    captures_failed: int
    captures_cancelled: int
    frames_captured: int
    frames_selected: int
    last_capture_at: str | None
    last_error: str | None
    barren_captures: int
    suspended: bool


class SwingCaptureService:
    """Turns each detected strike into a small set of stills of the replay."""

    def __init__(
        self,
        client_factory: ClientFactory,
        sources: Sequence[str],
        store: LatestSwingStore,
        publish: Publish,
        *,
        enabled: bool = False,
        initial_delay_seconds: float = 0.5,
        duration_seconds: float = 4.0,
        fps: float = 10.0,
        downswing_fps: float = 0.0,
        downswing_delay_seconds: float = 3.3,
        downswing_duration_seconds: float = 1.1,
        selected_frames: int = 8,
        image_width: int | None = None,
        panels: Sequence[CapturePanel] | None = None,
        static_delay_seconds: float = 3.5,
        static_duration_seconds: float = 1.5,
        static_fps: float = 2.0,
        metrics_source: str = "",
        metrics_delay_seconds: float = 3.0,
        capture_scene: str = CAPTURE_SCENE,
        command_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        # Not `time.monotonic`: before Python 3.13 that is GetTickCount64 on
        # Windows and moves in 15.6 ms steps, which is a third of a downswing
        # still's spacing and is written into every still's offset.
        monotonic: Callable[[], float] = time.perf_counter,
        now: Now = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
        announce_progress: bool = False,
        diagnostic_timeline_shots: int = 0,
    ) -> None:
        if diagnostic_timeline_shots < 0:
            raise ValueError("diagnostic_timeline_shots must be >= 0")
        if initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be >= 0")
        if metrics_delay_seconds < 0:
            raise ValueError("metrics_delay_seconds must be >= 0")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if fps <= 0:
            raise ValueError("fps must be > 0")
        if downswing_fps < 0:
            raise ValueError("downswing_fps must be >= 0")
        if downswing_delay_seconds < 0:
            raise ValueError("downswing_delay_seconds must be >= 0")
        if downswing_duration_seconds <= 0:
            raise ValueError("downswing_duration_seconds must be > 0")
        if selected_frames <= 0:
            raise ValueError("selected_frames must be > 0")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be > 0")

        self._factory = client_factory
        # Named camera_1, camera_2 … in the order they were configured. The
        # names are directory names and appear in every annotation later, so
        # they are positional and stable rather than derived from whatever
        # somebody called the source in OBS.
        named = tuple(
            CapturePanel(
                panel_id=f"camera_{position}",
                source=source.strip(),
                kind=PanelKind.DYNAMIC,
            )
            for position, source in enumerate(sources, start=1)
            if source.strip()
        )
        self._panels = tuple(panels) if panels is not None else named
        self._cameras = tuple(
            (panel.panel_id, panel.source) for panel in self._panels if panel.source
        )
        self._store = store
        self._publish = publish
        self._requested = enabled
        if enabled and not self._cameras:
            self._skip_reason = (
                "no panel source configured; set "
                "SWING_CAPTURE_PANEL_SWING_BEHIND_SOURCE, "
                "SWING_CAPTURE_PANEL_SWING_SIDE_SOURCE, "
                "SWING_CAPTURE_PANEL_IMPACT_SOURCE or "
                "SWING_CAPTURE_PANEL_DELIVERY_SOURCE"
            )
        else:
            self._skip_reason = ""
        self._enabled = enabled and bool(self._cameras)
        self._delay = initial_delay_seconds
        self._duration = duration_seconds
        self._fps = fps
        self._downswing = (
            Segment(
                downswing_delay_seconds,
                downswing_delay_seconds + downswing_duration_seconds,
                downswing_fps,
            )
            if downswing_fps > fps
            else None
        )
        self._selected = selected_frames
        self._static_delay = static_delay_seconds
        self._static_duration = static_duration_seconds
        self._static_fps = static_fps
        self._metrics_source = metrics_source.strip()
        self._metrics_delay = metrics_delay_seconds
        self._capture_scene = capture_scene
        self._last_metrics_digest: bytes | None = None
        self._image_width = image_width
        self._command_timeout = command_timeout
        self._shutdown_timeout = shutdown_timeout
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep
        # Whether a burst is the first stage of coaching. Only the application
        # knows that a coach is listening; a capture taken for nobody must not
        # put "capturing" on the floor and leave it for the ceiling to clear.
        self._announce_progress = announce_progress
        # How many more published shots keep every body screenshot. Counted
        # down only when a shot reaches the disk, so a capture superseded by a
        # newer strike does not use one up.
        self._diagnostic_remaining = diagnostic_timeline_shots
        self._burst_clock: dict[UUID, datetime] = {}
        self._cadence: dict[UUID, dict[str, Cadence]] = {}

        self._client: ScreenshotClient | None = None
        self._connection = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._publishing = asyncio.Lock()
        self._last_trigger_id: str | None = None
        self._latest: SwingCapture | None = None
        self._shot_ids: dict[str, str] = {}
        # Source names OBS said it does not have, during the capture in
        # flight. Cleared per capture so a source that comes back is noticed.
        self._missing: set[str] = set()
        self._invalid_sources: set[str] = set()
        self._barren = 0
        self._suspended = False
        self._skipped_while_suspended = 0

        self.captures_started = 0
        self.captures_completed = 0
        self.captures_failed = 0
        self.latest_failures = 0
        """`latest_swing` could not be replaced; the capture itself completed."""

        self.captures_cancelled = 0
        self.frames_captured = 0
        self.frames_selected = 0
        self.metrics_captured = 0
        self.metrics_failed = 0
        self.last_capture_at: datetime | None = None
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def latest(self) -> SwingCapture | None:
        """The stills currently on disk, or None if none have been taken.

        The seam a coaching pipeline reads. It is a reference to files rather
        than the bytes themselves: the frames of a swing are megabytes, they
        are already on disk, and holding them in memory for a consumer that may
        never arrive is how a subsystem starts costing something.
        """

        return self._latest

    @property
    def evidence_root(self) -> Path:
        """Where each shot's own capture directory lives, for anything that
        has to find a frame again by its capture and its id."""

        return self._store.evidence_root

    def subscribe(self, bus: EventBus) -> None:
        if not self._enabled and not self._skip_reason:
            return
        bus.subscribe(ShotDetected, self.on_shot_detected, name="swing_capture")
        if self._enabled:
            bus.subscribe(ShotCompleted, self.on_shot_completed, name="swing_capture")

    def start(self) -> None:
        """Say what is configured and sweep anything a crash left behind.

        Synchronous and cheap: no OBS connection is made here. The first
        screenshot of the session opens one, which means an OBS that is missing
        or slow costs the first capture rather than the start-up of the floor.
        """

        if not self._enabled:
            logger.info(
                "swing_capture_skipped"
                if self._skip_reason
                else "swing_capture_disabled",
                extra={
                    "swing_capture_detail": self._skip_reason
                    or "SWING_CAPTURE_ENABLED is false",
                },
            )
            return
        try:
            self._store.prepare()
        except OSError as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "swing_capture_directory_unavailable",
                extra={"swing_capture_directory": str(self._store.directory)},
                exc_info=error,
            )
        logger.info(
            "swing_capture_enabled",
            extra={
                "swing_capture_sources": ", ".join(
                    source for _camera, source in self._cameras
                ),
                "swing_capture_directory": str(self._store.directory),
                "swing_capture_delay_seconds": self._delay,
                "swing_capture_duration_seconds": self._duration,
                "swing_capture_fps": self._fps,
                "swing_capture_downswing_fps": (
                    None if self._downswing is None else self._downswing.fps
                ),
                "swing_capture_detail": (
                    "downswing off"
                    if self._downswing is None
                    else f"downswing {self._downswing.start:g}-"
                    f"{self._downswing.end:g}s; achieved cadence is logged "
                    "per shot as swing_capture_cadence"
                ),
                "swing_capture_selected": self._selected,
            },
        )
        if self._diagnostic_remaining:
            logger.warning(
                "swing_capture_diagnostic_timeline_enabled",
                extra={
                    "swing_capture_diagnostic_shots": self._diagnostic_remaining,
                    "swing_capture_detail": (
                        "every swing_behind and swing_side screenshot already "
                        "taken is also written under each shot's diagnostic/ "
                        "directory; nothing extra is requested from OBS or "
                        "sent to a model"
                    ),
                },
            )

    async def aclose(self) -> None:
        """Cancel a capture in flight and let go of OBS. Never raises."""

        if not self._enabled:
            return
        self._generation += 1
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                async with asyncio.timeout(self._shutdown_timeout):
                    await task
            except asyncio.CancelledError:
                # Expected: this is what cancelling it looks like.
                pass
            except TimeoutError:
                logger.warning("swing_capture_shutdown_timed_out")
            except Exception:
                logger.warning("swing_capture_shutdown_failed", exc_info=True)
        await self._drop_client()

    # --- the event path ----------------------------------------------------

    async def on_shot_detected(self, event: ShotDetected) -> None:
        """Start a capture. Returns before any of it: the bus must not wait."""

        if not self._enabled:
            if self._skip_reason:
                logger.info(
                    "swing_capture_skipped",
                    extra={
                        "shot_trigger_id": event.trigger_id,
                        "swing_capture_detail": self._skip_reason,
                    },
                )
            return
        if self._suspended:
            self._skipped_while_suspended += 1
            if self._skipped_while_suspended < _SHOTS_BEFORE_TRYING_AGAIN:
                logger.info(
                    "swing_capture_skipped",
                    extra={
                        "shot_trigger_id": event.trigger_id,
                        "swing_capture_barren": self._barren,
                        "swing_capture_detail": (
                            "suspended after repeated capture failures"
                        ),
                    },
                )
                return
            # Long enough to be worth one more look. Suspension exists so a
            # misconfigured bay does not reach for OBS on every strike, not so
            # that CaddieJ needs restarting: the ordinary Windows start has
            # OBS still coming up while CaddieJ is already running, and three
            # barren captures inside that window used to cost the whole
            # session its only pictures of a swing.
            logger.info(
                "swing_capture_retrying",
                extra={"swing_capture_barren": self._barren},
            )
            self._suspended = False
            self._barren = 0
            self._skipped_while_suspended = 0
        trigger_id = event.trigger_id
        if trigger_id and trigger_id == self._last_trigger_id:
            # A log the observer had to re-read from the start replays the
            # detections in it. One capture per strike.
            logger.info(
                "swing_capture_ignored_duplicate",
                extra={"shot_trigger_id": trigger_id},
            )
            return
        self._last_trigger_id = trigger_id or None

        self._generation += 1
        generation = self._generation
        previous = self._task
        if previous is not None and not previous.done():
            # Not awaited: this runs on a bus worker. The cancelled capture's
            # generation is already stale, so nothing it had left to do can
            # reach the disk.
            self.captures_cancelled += 1
            previous.cancel()
            logger.info(
                "swing_capture_superseded",
                extra={"shot_trigger_id": trigger_id},
            )

        self.captures_started += 1
        logger.info(
            "swing_capture_triggered",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_delay_seconds": self._delay,
                "swing_capture_sources": ", ".join(
                    f"{panel.panel_id}={panel.source}" for panel in self._panels
                ),
            },
        )
        self._task = asyncio.create_task(
            self._run(generation, trigger_id, event.occurred_at, event.event_id),
            name="swing-capture",
        )

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Remember which shot this trigger became, once GSPro has written it."""

        trigger_id = event.trigger_id
        if not trigger_id:
            return
        shot_id = str(event.shot.shot_id)
        self._shot_ids[trigger_id] = shot_id
        while len(self._shot_ids) > 8:
            self._shot_ids.pop(next(iter(self._shot_ids)))
        if self._latest is None or self._latest.trigger_id != trigger_id:
            return
        await self._note_shot(trigger_id, shot_id)

    # --- the capture -------------------------------------------------------

    async def _run(
        self,
        generation: int,
        trigger_id: str,
        detected_at: datetime | None,
        strike_id: UUID | None = None,
    ) -> None:
        capture_id = uuid4()
        self._missing.clear()
        await self._announce_capturing(trigger_id, strike_id)
        try:
            started_at = self._now()
            tracks, failures, metrics = await self._capture(
                generation, trigger_id, capture_id, detected_at
            )
            await self._finish(
                generation,
                capture_id,
                trigger_id,
                detected_at,
                started_at,
                tracks,
                failures,
                strike_id=strike_id,
                metrics=metrics,
            )
        except asyncio.CancelledError:
            logger.info(
                "swing_capture_cancelled", extra={"shot_trigger_id": trigger_id}
            )
            raise
        except Exception as error:
            self.captures_failed += 1
            self.last_error = f"{type(error).__name__}: {error}"
            logger.exception(
                "swing_capture_failed",
                extra={
                    "shot_trigger_id": trigger_id,
                    "swing_capture_id": str(capture_id),
                },
            )
            self._barren_capture(trigger_id)
        finally:
            self._burst_clock.pop(capture_id, None)
            self._cadence.pop(capture_id, None)

    async def _announce_capturing(
        self, trigger_id: str, strike_id: UUID | None
    ) -> None:
        """Say the burst is running. A failure to say so costs nothing else.

        On the capture task rather than the bus worker, so the strike has
        already been handed back before anything is published, and outside the
        capture's own error handling, so an announcement that fails can never
        be counted as a capture that did.
        """

        if not self._announce_progress:
            return
        try:
            await self._publish(
                CoachingProgress(
                    stage=CoachingStage.CAPTURING,
                    trigger_id=trigger_id,
                    shot_id=strike_id,
                )
            )
        except Exception:
            logger.warning(
                "swing_capture_progress_failed",
                extra={"shot_trigger_id": trigger_id},
                exc_info=True,
            )

    def _barren_capture(self, trigger_id: str) -> None:
        """Count a capture that produced nothing, and stand down after enough.

        Standing down rather than giving up. Reaching for OBS on every strike
        is what a subsystem nothing depends on must not do, but the two things
        that cause this — a source name that does not exist, and an OBS that
        is not running — are both fixed while CaddieJ is running, and one of
        them fixes itself: OBS finishing its own start-up. This used to hold
        until the process was restarted, so three barren captures in the first
        minute cost the evening every picture of a swing, which for a coach
        asked to describe one is the whole of what it had.
        """

        self._barren += 1
        if self._barren < _CAPTURES_BEFORE_GIVING_UP or self._suspended:
            return
        self._suspended = True
        self._skipped_while_suspended = 0
        logger.error(
            "swing_capture_suspended",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_sources": ", ".join(
                    source for _camera, source in self._cameras
                ),
                "swing_capture_barren": self._barren,
                "swing_capture_detail": self.last_error,
            },
        )

    async def _capture(
        self,
        generation: int,
        trigger_id: str,
        capture_id: UUID,
        detected_at: datetime | None = None,
    ) -> tuple[dict[str, list[CapturedFrame]], dict[str, int], MetricsResult | None]:
        """Ask each panel for stills on its own schedule, one generation.

        Dynamic panels share one burst. Static panels start later and more
        slowly, so a Top Impact graphic can finish drawing before we treat a
        still as settled. The generation is checked every tick, so a newer
        strike abandons every panel at once.
        """

        tracks: dict[str, list[CapturedFrame]] = {
            panel.panel_id: [] for panel in self._panels
        }
        failures: dict[str, int] = {panel.panel_id: 0 for panel in self._panels}
        self._invalid_sources.clear()
        client = await self._connected()
        hold = getattr(client, "hold_sources", None)
        sources = tuple(panel.source for panel in self._panels)
        if self._metrics_source and self._metrics_source not in sources:
            sources += (self._metrics_source,)
        if hold is None:
            return await self._capture_all(
                generation, trigger_id, capture_id, detected_at, tracks, failures
            )
        async with hold(sources, capture_scene=self._capture_scene):
            await self._log_source_state(client, trigger_id, sources)
            return await self._capture_all(
                generation, trigger_id, capture_id, detected_at, tracks, failures
            )

    async def _log_source_state(
        self, client: object, trigger_id: str, sources: tuple[str, ...]
    ) -> None:
        """Whether OBS is rendering each source as the burst starts.

        A source that is not showing is not refreshed, and every screenshot of
        it is the same cached picture. This line is what tells that apart from
        a replay that had not started.
        """

        query = getattr(client, "source_showing", None)
        if query is None:
            return
        states: list[str] = []
        for source in sources:
            try:
                showing = await query(source)
            except Exception:
                showing = None
            states.append(
                f"{source}={'unknown' if showing is None else showing}".lower()
            )
        logger.info(
            "swing_capture_sources_armed",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_detail": "showing: " + ", ".join(states),
            },
        )

    async def _capture_all(
        self,
        generation: int,
        trigger_id: str,
        capture_id: UUID,
        detected_at: datetime | None,
        tracks: dict[str, list[CapturedFrame]],
        failures: dict[str, int],
    ) -> tuple[dict[str, list[CapturedFrame]], dict[str, int], MetricsResult | None]:
        """The burst, and alongside it the one All Values still.

        Both run inside the one source hold, so a hidden ProTee window is armed
        for both and restored once, after whichever finishes last.
        """

        metrics_task: asyncio.Task[MetricsResult] | None = None
        if self._metrics_source:
            metrics_task = asyncio.create_task(
                self._capture_metrics(generation, trigger_id, detected_at),
                name="coach-metrics-capture",
            )
        try:
            tracks, failures = await self._capture_frames(
                generation, trigger_id, capture_id, tracks, failures
            )
        except BaseException:
            if metrics_task is not None:
                metrics_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await metrics_task
            raise
        metrics = None if metrics_task is None else await metrics_task
        return tracks, failures, metrics

    async def _capture_metrics(
        self, generation: int, trigger_id: str, detected_at: datetime | None
    ) -> MetricsResult:
        """Exactly one still of the All Values grid, at a fixed delay.

        The delay runs from the start of the capture, which is the strike, so
        the still is taken at the same point after every shot. A still that is
        byte-identical to the previous shot's is ProTee not having redrawn yet,
        not a reading, and it is refused.
        """

        source = self._metrics_source
        delay = self._metrics_delay
        logger.info(
            "coach_metrics_capture_triggered",
            extra={
                "shot_trigger_id": trigger_id,
                "coach_metrics_source": source,
                "coach_metrics_delay_seconds": delay,
            },
        )
        if delay > 0:
            await self._sleep(delay)
        data: bytes | None = None
        size: tuple[int, int] | None = None
        detail: str | None = None
        digest: bytes | None = None
        for attempt in range(2):
            if generation != self._generation:
                raise asyncio.CancelledError
            if attempt:
                await self._sleep(_METRICS_RETRY_SECONDS)
                if generation != self._generation:
                    raise asyncio.CancelledError
            data, size, detail = await self._metrics_still(source)
            if data is not None:
                digest = hashlib.sha256(data).digest()
                if digest == self._last_metrics_digest:
                    data = None
                    detail = (
                        "identical to the previous shot's All Values still; "
                        "ProTee had not redrawn the grid"
                    )
            if data is not None or source in self._missing:
                break
        captured_at = self._now()
        offset = (
            None
            if detected_at is None
            else round((captured_at - detected_at).total_seconds(), 3)
        )
        if data is None or size is None:
            self.metrics_failed += 1
            logger.warning(
                "coach_metrics_capture_failed",
                extra={
                    "shot_trigger_id": trigger_id,
                    "coach_metrics_source": source,
                    "coach_metrics_status": "unavailable",
                    "coach_metrics_delay_seconds": delay,
                    "coach_metrics_offset_seconds": offset,
                    "swing_capture_detail": detail,
                },
            )
            return MetricsResult(
                source=source,
                ok=False,
                captured_at=captured_at,
                delay_seconds=delay,
                offset_seconds=offset,
                detail=detail or "no still returned",
            )
        self._last_metrics_digest = digest
        self.metrics_captured += 1
        logger.info(
            "coach_metrics_capture_completed",
            extra={
                "shot_trigger_id": trigger_id,
                "coach_metrics_source": source,
                "coach_metrics_status": "captured",
                "coach_metrics_delay_seconds": delay,
                "coach_metrics_offset_seconds": offset,
                "coach_metrics_captured_at": captured_at.isoformat(),
                "coach_metrics_width": size[0],
                "coach_metrics_height": size[1],
            },
        )
        return MetricsResult(
            source=source,
            ok=True,
            data=data,
            width=size[0],
            height=size[1],
            captured_at=captured_at,
            delay_seconds=delay,
            offset_seconds=offset,
        )

    async def _metrics_still(
        self, source: str
    ) -> tuple[bytes | None, tuple[int, int] | None, str | None]:
        """The All Values screenshot at the source's own resolution.

        Not downscaled to the swing-frame width: these are small digits, and
        the coach has to read them.
        """

        timeout = max(
            _MINIMUM_FRAME_TIMEOUT, min(self._command_timeout, _METRICS_TIMEOUT)
        )
        try:
            async with asyncio.timeout(timeout):
                client = await self._connected()
                data = await client.source_screenshot(source)
                size = require_usable_screenshot(source, data)
                return data, size, None
        except asyncio.CancelledError:
            raise
        except UnusableScreenshot as error:
            logger.warning(
                "invalid_capture_dimensions",
                extra={
                    "swing_capture_source": source,
                    "swing_capture_detail": str(error),
                },
            )
            return None, None, str(error)
        except ObsResourceNotFoundError:
            self._missing.add(source)
            return None, None, f"OBS has no source named {source!r}"
        except Exception as error:
            await self._drop_client()
            return None, None, f"{type(error).__name__}: {error}"

    async def _capture_frames(
        self,
        generation: int,
        trigger_id: str,
        capture_id: UUID,
        tracks: dict[str, list[CapturedFrame]],
        failures: dict[str, int],
    ) -> tuple[dict[str, list[CapturedFrame]], dict[str, int]]:
        """The timed burst. Sources are already showing when this starts.

        Every panel's stills are due at fixed offsets from `began`
        (`cadence.sample_times`); the loop sleeps until the earliest one and
        asks every panel due by then, together, as it always has. A request
        that comes back after later deadlines have passed skips all but the
        latest of them, so nothing queues behind a slow OBS. No panel is asked
        again sooner than `cadence.MIN_SPACING` of its period after its last
        request (`cadence.spacing_floor`).
        """

        windows = {panel.panel_id: self._window(panel) for panel in self._panels}
        plans = {panel.panel_id: self._plan(panel) for panel in self._panels}
        position = dict.fromkeys(plans, 0)
        asked: dict[str, list[float]] = {panel_id: [] for panel_id in plans}
        latency: dict[str, list[float]] = {panel_id: [] for panel_id in plans}
        missed = dict.fromkeys(plans, 0)
        overruns = dict.fromkeys(plans, 0)
        tick = max(
            (self._rate(panel) for panel in self._panels),
            default=self._fps,
        )
        interval = 1.0 / tick
        timeout = max(
            _MINIMUM_FRAME_TIMEOUT,
            min(self._command_timeout, interval * _TIMEOUT_TICKS),
        )
        end = max(
            (start + length for start, length, _rate in windows.values()), default=0.0
        )
        began = self._monotonic()
        if self._diagnostic_remaining:
            self._burst_clock[capture_id] = self._now()
        logger.info(
            "swing_capture_started",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_id": str(capture_id),
                "swing_capture_frames": max(
                    (len(plan) for plan in plans.values()), default=0
                ),
                "swing_capture_fps": tick,
                "swing_capture_duration_seconds": end,
                # The burst lasts until the last panel's window ends, which is
                # the static panel's when it is sampled later than the body.
                "swing_capture_detail": "; ".join(
                    f"{panel.panel_id} {start:g}-{start + length:g}s @{rate:g}fps"
                    + self._downswing_note(panel)
                    for panel in self._panels
                    for start, length, rate in (windows[panel.panel_id],)
                ),
            },
        )
        spacing_holds = dict.fromkeys(plans, 0)
        holding: set[str] = set()
        frame_index = 0
        while True:
            pending = {
                panel_id: (
                    plan[position[panel_id]],
                    spacing_floor(
                        plan,
                        position[panel_id],
                        asked[panel_id][-1] if asked[panel_id] else None,
                    ),
                )
                for panel_id, plan in plans.items()
                if position[panel_id] < len(plan)
            }
            if not pending:
                break
            elapsed = self._monotonic() - began
            ready = {
                panel_id
                for panel_id, (deadline, floor) in pending.items()
                if deadline <= elapsed + EARLY_SECONDS and elapsed >= floor - 1e-9
            }
            if not ready:
                # Each panel waits for its deadline, as it always did, or --
                # when the deadline has come and its last request was too
                # recent -- for its spacing. That wait is at least one
                # early-fire allowance, so it is never shorter than a tick.
                waits = []
                for panel_id, (deadline, floor) in pending.items():
                    if deadline > elapsed + EARLY_SECONDS:
                        waits.append(deadline - elapsed)
                    else:
                        holding.add(panel_id)
                        waits.append(max(floor - elapsed, EARLY_SECONDS))
                await self._sleep(min(waits))
                if generation != self._generation:
                    raise asyncio.CancelledError
                continue
            if generation != self._generation:
                raise asyncio.CancelledError
            due_panels = [panel for panel in self._panels if panel.panel_id in ready]
            for panel in due_panels:
                if panel.panel_id in holding:
                    holding.discard(panel.panel_id)
                    spacing_holds[panel.panel_id] += 1
            stills = await asyncio.gather(
                *(self._still(panel.source, timeout) for panel in due_panels)
            )
            answered = self._monotonic() - began
            for panel, still in zip(due_panels, stills, strict=True):
                panel_id = panel.panel_id
                plan = plans[panel_id]
                asked[panel_id].append(elapsed)
                latency[panel_id].append(answered - elapsed)
                position[panel_id] += 1
                if (
                    position[panel_id] < len(plan)
                    and plan[position[panel_id]] < answered
                ):
                    overruns[panel_id] += 1
                while (
                    position[panel_id] + 1 < len(plan)
                    and plan[position[panel_id] + 1] <= answered
                ):
                    position[panel_id] += 1
                    missed[panel_id] += 1
                if still is None:
                    failures[panel_id] += 1
                    continue
                tracks[panel_id].append(
                    CapturedFrame(offset_seconds=elapsed, data=still)
                )
                self.frames_captured += 1
            logger.debug(
                "swing_capture_frame",
                extra={
                    "shot_trigger_id": trigger_id,
                    "swing_capture_frame": frame_index + 1,
                    "swing_capture_offset_seconds": round(elapsed, 3),
                    "swing_capture_frames": sum(
                        len(items) for items in tracks.values()
                    ),
                    "swing_capture_panel": ",".join(
                        panel.panel_id for panel in due_panels
                    ),
                },
            )
            frame_index += 1
        self._cadence[capture_id] = {
            panel.panel_id: summarise(
                requested_fps=windows[panel.panel_id][2],
                downswing=self._segment(panel),
                planned=len(plans[panel.panel_id]),
                offsets=asked[panel.panel_id],
                latencies=latency[panel.panel_id],
                missed=missed[panel.panel_id],
                overruns=overruns[panel.panel_id],
                spacing_holds=spacing_holds[panel.panel_id],
            )
            for panel in self._panels
        }
        return tracks, failures

    def _window(self, panel: CapturePanel) -> tuple[float, float, float]:
        """Delay, duration and sample rate for one panel."""

        if panel.kind is PanelKind.STATIC:
            return self._static_delay, self._static_duration, self._static_fps
        return self._delay, self._duration, self._fps

    def _rate(self, panel: CapturePanel) -> float:
        return self._window(panel)[2]

    def _segment(self, panel: CapturePanel) -> Segment | None:
        """The faster downswing stretch, for the body replays only."""

        return self._downswing if panel.kind is PanelKind.DYNAMIC else None

    def _plan(self, panel: CapturePanel) -> tuple[float, ...]:
        delay, duration, rate = self._window(panel)
        return sample_times(delay, duration, rate, self._segment(panel))

    def _downswing_note(self, panel: CapturePanel) -> str:
        segment = self._segment(panel)
        if segment is None:
            return ""
        return f" (+{segment.start:g}-{segment.end:g}s @{segment.fps:g}fps)"

    async def _still(self, source: str, timeout: float) -> bytes | None:
        """One screenshot, bounded. Every failure is a None and a log line."""

        try:
            async with asyncio.timeout(timeout):
                client = await self._connected()
                data = await client.source_screenshot(source, width=self._image_width)
                require_usable_screenshot(source, data)
                return data
        except asyncio.CancelledError:
            raise
        except UnusableScreenshot as error:
            if source not in self._invalid_sources:
                self._invalid_sources.add(source)
                logger.warning(
                    "invalid_capture_dimensions",
                    extra={
                        "swing_capture_source": source,
                        "swing_capture_detail": str(error),
                    },
                )
            self.last_error = str(error)
            return None
        except ObsResourceNotFoundError:
            # The socket is fine. The source name is wrong, and it will still
            # be wrong on the next frame. Throwing the connection away here is
            # what turned two misconfigured camera names into nineteen
            # thousand OBS handshakes in a single session, because every frame
            # of every capture reconnected before asking for a source that was
            # never going to exist.
            self._missing.add(source)
            self.last_error = f"missing source {source!r}"
            return None
        except Exception as error:
            # A connection that is down, a request that hung: the client is
            # suspect, so it goes. Which failure it was is kept for health and
            # named once per capture rather than once per frame — an OBS that
            # is not running would otherwise write forty warnings per shot.
            self.last_error = f"{type(error).__name__}: {error}"
            logger.debug(
                "swing_capture_frame_failed",
                extra={"swing_capture_source": source},
                exc_info=error,
            )
            await self._drop_client()
            return None

    async def _finish(
        self,
        generation: int,
        capture_id: UUID,
        trigger_id: str,
        detected_at: datetime | None,
        started_at: datetime,
        tracks: dict[str, list[CapturedFrame]],
        failures: dict[str, int],
        *,
        strike_id: UUID | None = None,
        metrics: MetricsResult | None = None,
    ) -> None:
        """Choose the frames worth keeping, write them, and say they exist.

        A capture with no swing frame but a usable All Values still is still
        published: the measured half of the coaching does not need the body.
        """

        chosen, intervals, combined, phases = self._select_panels(tracks)
        diagnostic = self._diagnostic_remaining > 0
        cadence = self._cadence.get(capture_id, {})
        cameras = tuple(
            self._result(
                panel,
                tracks,
                chosen,
                intervals,
                failures,
                phases,
                diagnostic,
                cadence.get(panel.panel_id),
            )
            for panel in self._panels
        )
        for camera in cameras:
            _log_cadence(trigger_id, camera)
            if not camera.ok:
                # Said once, clearly, and not fatal: the other panel is still
                # a swing worth looking at.
                logger.warning(
                    "swing_capture_camera_unavailable",
                    extra={
                        "swing_capture_camera": camera.camera_id,
                        "swing_capture_source": camera.source,
                        "swing_capture_failed": camera.failed,
                        "swing_capture_detail": camera.detail,
                    },
                )
            _log_replay_start(trigger_id, camera)
            first_change, last_change = _changes(camera)
            dynamic = camera.kind is PanelKind.DYNAMIC
            if dynamic and camera.captured >= 3 and camera.distinct_frames == 1:
                logger.warning(
                    "swing_capture_source_frozen",
                    extra={
                        "shot_trigger_id": trigger_id,
                        "swing_capture_panel": camera.camera_id,
                        "swing_capture_source": camera.source,
                        "swing_capture_frames": camera.captured,
                        "swing_capture_distinct_frames": camera.distinct_frames,
                        "swing_capture_detail": (
                            "every screenshot was byte-identical: OBS rendered "
                            "the same texture each time. Either the source was "
                            "not refreshing or the replay did not move inside "
                            "the window; one still is kept, not copies"
                        ),
                    },
                )
            logger.info(
                "swing_replay_interval_detected"
                if camera.interval is not None and camera.interval.detected
                else "swing_replay_interval_not_detected",
                extra={
                    "swing_capture_camera": camera.camera_id,
                    "swing_capture_panel": camera.camera_id,
                    "swing_capture_source": camera.source,
                    # Only a static panel settles. A body panel is a moving
                    # replay and has no settled still to report.
                    "swing_capture_settled": None if dynamic else camera.settled,
                    "swing_capture_frames": camera.captured,
                    "swing_capture_distinct_frames": camera.distinct_frames,
                    "swing_capture_first_change_seconds": first_change,
                    "swing_capture_last_change_seconds": last_change,
                    "swing_capture_failed": camera.failed,
                    "swing_capture_interval_start": (
                        None
                        if camera.interval is None
                        else round(camera.interval.start_seconds, 3)
                    ),
                    "swing_capture_interval_end": (
                        None
                        if camera.interval is None
                        else round(camera.interval.end_seconds, 3)
                    ),
                    "swing_capture_pre_replay_frames": (
                        None
                        if camera.interval is None
                        else camera.interval.pre_replay_frames
                    ),
                },
            )

        kept = sum(len(camera.frames) for camera in cameras)
        captured, blank, duplicate = _frame_accounting(tracks)
        sources = ", ".join(
            f"{panel.panel_id}={panel.source}" for panel in self._panels
        )
        accounting = (
            f"captured={captured}; blank_rejected={blank}; "
            f"duplicate_frames={duplicate}; kept={kept}; sources={sources}"
        )
        if kept == 0:
            self.captures_failed += 1
            logger.error(
                "swing_capture_failed",
                extra={
                    "shot_trigger_id": trigger_id,
                    "swing_capture_id": str(capture_id),
                    "swing_capture_detail": (
                        "no camera produced a usable still; " + accounting
                    ),
                },
            )
            self._barren_capture(trigger_id)
            if metrics is None or not metrics.ok:
                return
        else:
            # One camera is enough to say the subsystem works, so the count
            # that decides whether to give up resets here and not on a partial
            # failure.
            self._barren = 0

        logger.info(
            "swing_frames_selected",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_id": str(capture_id),
                "swing_capture_selected": kept,
                "swing_capture_interval_start": round(combined.start_seconds, 3),
                "swing_capture_interval_end": round(combined.end_seconds, 3),
                "swing_capture_interval_detected": combined.detected,
                "swing_capture_pre_replay_frames": combined.pre_replay_frames,
                "swing_capture_detail": accounting,
                "swing_capture_sources": sources,
            },
        )

        record = CaptureRecord(
            capture_id=capture_id,
            trigger_id=trigger_id,
            detected_at=detected_at,
            started_at=started_at,
            completed_at=self._now(),
            cameras=cameras,
            interval=combined,
            configuration=self._configuration(),
            strike_id=strike_id,
            metrics=metrics,
            burst_started_at=self._burst_clock.get(capture_id),
        )
        publication = await self._store_capture(generation, record)
        if publication is None:
            return
        capture = publication.capture
        if diagnostic:
            self._diagnostic_retained(trigger_id, capture, cameras)
        if publication.latest_ok:
            logger.info(
                "swing_capture_latest_published",
                extra={
                    "shot_trigger_id": trigger_id,
                    "swing_capture_id": str(capture_id),
                    "swing_capture_directory": str(capture.published),
                },
            )
        else:
            self.latest_failures += 1
            logger.warning(
                "swing_capture_latest_publish_failed",
                extra={
                    "shot_trigger_id": trigger_id,
                    "swing_capture_id": str(capture_id),
                    "swing_capture_directory": str(capture.directory),
                    "swing_capture_detail": (
                        f"{publication.latest_detail}; the capture itself is "
                        "complete and coaching reads it from swing_directory. "
                        "latest_swing still holds an older shot and is marked "
                        "with STALE.json. On Windows this means something has a "
                        "file inside latest_swing open"
                    )[:400],
                },
            )

        self.captures_completed += 1
        self.frames_selected += kept
        self.last_capture_at = record.completed_at
        self._latest = capture
        logger.info(
            "swing_capture_completed",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_id": str(capture_id),
                "swing_capture_directory": str(capture.directory),
                "swing_capture_selected": kept,
                "swing_capture_elapsed_seconds": round(
                    (record.completed_at - record.started_at).total_seconds(), 3
                ),
                "swing_capture_detail": accounting,
                "swing_capture_sources": sources,
                "coaching_strike_id": None if strike_id is None else str(strike_id),
                "coach_metrics_status": _metrics_status(capture),
            },
        )
        await self._publish(SwingFramesReady(capture=capture))
        shot_id = self._shot_ids.get(trigger_id)
        if shot_id is not None:
            await self._note_shot(trigger_id, shot_id)

    def _select_panels(
        self, tracks: dict[str, list[CapturedFrame]]
    ) -> tuple[
        dict[str, tuple[int, ...]],
        dict[str, ActiveInterval],
        ActiveInterval,
        dict[str, tuple[SwingPhase, ...]],
    ]:
        """Each dynamic panel is aimed across its own replay; static panels settle."""

        # Distinct pictures, not screenshots: a panel that returned one frozen
        # picture forty times has one still to offer, and its unused share is
        # better spent on a panel that moved. A calibrated camera can use one
        # still per moment it is aimed at and no more; what is left over is
        # given to another panel or not spent.
        usable = {
            panel.panel_id: _usable_stills(panel, tracks.get(panel.panel_id, []))
            for panel in self._panels
        }
        quota = package_quota(
            self._selected,
            tuple(
                (panel.panel_id, panel.kind is PanelKind.STATIC, usable[panel.panel_id])
                for panel in self._panels
            ),
        )
        dynamic = {
            panel.panel_id: tracks.get(panel.panel_id, [])
            for panel in self._panels
            if panel.kind is PanelKind.DYNAMIC and quota.get(panel.panel_id, 0) > 0
        }
        chosen: dict[str, tuple[int, ...]] = {}
        intervals: dict[str, ActiveInterval] = {}
        phases: dict[str, tuple[SwingPhase, ...]] = {}
        combined = ActiveInterval(0.0, 0.0, detected=False)
        if dynamic:
            # Each panel is asked for exactly its own quota, across its own
            # replay, aimed at the phases its own view can show. The previous
            # shape -- ask every panel for the widest quota, then thin each
            # back down with `take_evenly` -- is what undid the aiming: a
            # thinning that keeps moments for their spacing will happily drop
            # the transition and keep the player walking away.
            chosen, intervals, combined, aimed = select(
                dynamic,
                {panel_id: quota.get(panel_id, 0) for panel_id in dynamic},
                {panel.panel_id: panel.view for panel in self._panels},
            )
            phases.update(aimed)
        for panel in self._panels:
            if panel.kind is not PanelKind.STATIC:
                continue
            if quota.get(panel.panel_id, 0) <= 0:
                chosen[panel.panel_id] = ()
                continue
            frames = tracks.get(panel.panel_id, [])
            choice = static_choice(frames)
            if choice is None:
                chosen[panel.panel_id] = ()
                continue
            index, settled = choice
            chosen[panel.panel_id] = (index,)
            intervals[panel.panel_id] = ActiveInterval(
                frames[0].offset_seconds,
                frames[-1].offset_seconds,
                detected=settled,
            )
        return chosen, intervals, combined, phases

    def _result(
        self,
        panel: CapturePanel,
        tracks: dict[str, list[CapturedFrame]],
        chosen: dict[str, tuple[int, ...]],
        intervals: dict[str, ActiveInterval],
        failures: dict[str, int],
        phases: Mapping[str, tuple[SwingPhase, ...]] | None = None,
        diagnostic: bool = False,
        cadence: Cadence | None = None,
    ) -> CameraResult:
        captured = tracks.get(panel.panel_id, [])
        indices = chosen.get(panel.panel_id, ())
        picked = tuple(captured[index] for index in indices)
        settled = False
        if panel.kind is PanelKind.STATIC and picked:
            _index, settled = settled_frame(captured)
        _captured, blank, duplicate = _frame_accounting({panel.panel_id: captured})
        return CameraResult(
            distinct_frames=distinct_count(captured),
            selected_indices=tuple(indices),
            phases=(phases or {}).get(panel.panel_id, ()),
            timeline=_timeline(captured, frozenset(indices)),
            camera_id=panel.panel_id,
            source=panel.source,
            ok=bool(picked),
            captured=len(captured),
            failed=failures.get(panel.panel_id, 0),
            frames=picked,
            interval=intervals.get(panel.panel_id),
            detail=self._camera_detail(panel, captured, picked),
            kind=panel.kind,
            supports=panel.supports,
            settled=settled,
            view=panel.view,
            blank_rejected=blank,
            duplicate_frames=duplicate,
            diagnostic_frames=(
                tuple(captured)
                if diagnostic and panel.view in _DIAGNOSTIC_VIEWS
                else ()
            ),
            cadence=cadence,
        )

    def _diagnostic_retained(
        self,
        trigger_id: str,
        capture: SwingCapture,
        cameras: Sequence[CameraResult],
    ) -> None:
        """Use up one diagnostic shot and say what it kept."""

        self._diagnostic_remaining = max(0, self._diagnostic_remaining - 1)
        root = capture.directory / DIAGNOSTIC_DIRECTORY
        retained = {
            camera.camera_id: sum(1 for _ in (root / camera.camera_id).glob("*.jpg"))
            for camera in cameras
            if camera.view in _DIAGNOSTIC_VIEWS
        }
        complete = all(
            retained[camera.camera_id] == camera.captured
            for camera in cameras
            if camera.view in _DIAGNOSTIC_VIEWS
        )
        logger.warning(
            "swing_capture_diagnostic_timeline_retained",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_id": str(capture.capture_id),
                "swing_capture_directory": str(root),
                "swing_capture_diagnostic_complete": complete,
                "swing_capture_diagnostic_shots": self._diagnostic_remaining,
                "swing_capture_detail": "; ".join(
                    f"{camera.camera_id}={retained[camera.camera_id]}"
                    f"/{camera.captured} captured"
                    for camera in cameras
                    if camera.view in _DIAGNOSTIC_VIEWS
                ),
            },
        )
        if not self._diagnostic_remaining:
            logger.warning(
                "swing_capture_diagnostic_timeline_finished",
                extra={
                    "swing_capture_detail": (
                        "the configured diagnostic shots are on disk; later "
                        "shots keep only their selected frames"
                    ),
                },
            )

    def _camera_detail(
        self,
        panel: CapturePanel,
        captured: Sequence[CapturedFrame],
        picked: tuple[CapturedFrame, ...],
    ) -> str | None:
        if picked:
            return None
        source = panel.source
        if source in self._missing:
            return f"OBS has no source named {source!r}"
        if (
            panel.kind is PanelKind.DYNAMIC
            and panel.view in _BODY_VIEWS
            and any(not likely_blank(frame.data) for frame in captured)
        ):
            return _NO_REPLAY_START
        return f"no still was returned for {source!r}"

    async def _store_capture(
        self, generation: int, record: CaptureRecord
    ) -> Publication | None:
        """Write the capture and swap it in, unless a newer swing owns the disk.

        The staleness check is inside the lock and after the write, because the
        write is the slow part: a capture that was still encoding JPEGs when
        the next ball was struck must not put them where the newer swing goes.
        """

        async with self._publishing:
            try:
                staged = await asyncio.to_thread(self._store.stage, record)
            except OSError as error:
                self.captures_failed += 1
                self.last_error = f"{type(error).__name__}: {error}"
                logger.error(
                    "swing_capture_failed",
                    extra={
                        "shot_trigger_id": record.trigger_id,
                        "swing_capture_id": str(record.capture_id),
                        "swing_capture_detail": str(error)[:300],
                    },
                )
                return None

            if generation != self._generation:
                self.captures_cancelled += 1
                await asyncio.to_thread(self._store.discard, staged)
                logger.info(
                    "swing_capture_discarded_stale",
                    extra={
                        "shot_trigger_id": record.trigger_id,
                        "swing_capture_id": str(record.capture_id),
                    },
                )
                return None

            try:
                return await asyncio.to_thread(self._store.publish, staged, record)
            except OSError as error:
                self.captures_failed += 1
                self.last_error = f"{type(error).__name__}: {error}"
                await asyncio.to_thread(self._store.discard, staged)
                logger.error(
                    "swing_capture_failed",
                    extra={
                        "shot_trigger_id": record.trigger_id,
                        "swing_capture_id": str(record.capture_id),
                        "swing_capture_detail": (
                            f"the evidence directory could not be written: {error}"
                        )[:300],
                    },
                )
                return None

    def _configuration(self) -> dict[str, object]:
        """What the capture was told to do, recorded beside what it produced.

        Written into the metadata because these values are guesses until a real
        shot corrects them, and a set of frames that missed the replay is only
        interpretable next to the timings that produced it.
        """

        return {
            "initial_delay_seconds": self._delay,
            "duration_seconds": self._duration,
            "fps": self._fps,
            "downswing_fps": None if self._downswing is None else self._downswing.fps,
            "downswing_delay_seconds": (
                None if self._downswing is None else self._downswing.start
            ),
            "downswing_duration_seconds": (
                None
                if self._downswing is None
                else round(self._downswing.end - self._downswing.start, 6)
            ),
            "selected_frames": self._selected,
            "static_delay_seconds": self._static_delay,
            "static_duration_seconds": self._static_duration,
            "static_fps": self._static_fps,
            "image_width": self._image_width,
            "coach_metrics_source": self._metrics_source or None,
            "coach_metrics_delay_seconds": (
                self._metrics_delay if self._metrics_source else None
            ),
            "sources": {camera: source for camera, source in self._cameras},
            "panels": [
                {
                    "panel_id": panel.panel_id,
                    "source": panel.source,
                    "kind": panel.kind.value,
                    "supports": sorted(claim.value for claim in panel.supports),
                    "view": panel.view.value,
                    "setting": panel.setting,
                }
                for panel in self._panels
            ],
        }

    async def _note_shot(self, trigger_id: str, shot_id: str) -> None:
        """Write the shot id next to the capture once both exist."""

        try:
            written = await asyncio.to_thread(
                self._store.note_shot, trigger_id, shot_id
            )
        except OSError:
            logger.warning(
                "swing_capture_shot_unrecorded",
                extra={"shot_trigger_id": trigger_id, "shot_id": shot_id},
                exc_info=True,
            )
            return
        if not written:
            return
        logger.info(
            "swing_capture_correlated",
            extra={"shot_trigger_id": trigger_id, "shot_id": shot_id},
        )

    # --- OBS ---------------------------------------------------------------

    async def _connected(self) -> ScreenshotClient:
        """The one client, opened on demand.

        Locked because both cameras ask for it at the same instant by design.
        Without the lock each of them sees no client, each opens one, and the
        loser's connection is overwritten with nothing holding a reference to
        it — a leaked WebSocket per reconnect, which OBS closes only when it
        times out.
        """

        if self._client is not None:
            return self._client
        async with self._connection:
            if self._client is not None:
                return self._client
            client = self._factory()
            await client.connect()
            self._client = client
            logger.info("swing_capture_obs_connected")
            return client

    async def _drop_client(self) -> None:
        """Throw the connection away. The next still opens a fresh one."""

        async with self._connection:
            client = self._client
            self._client = None
            if client is None:
                return
            try:
                await client.disconnect()
            except Exception:
                logger.debug("swing_capture_disconnect_failed", exc_info=True)

    # --- reporting ---------------------------------------------------------

    def health(self) -> SwingCaptureHealth:
        return SwingCaptureHealth(
            enabled=self._enabled,
            sources=tuple(source for _camera, source in self._cameras),
            panels=tuple(panel.panel_id for panel in self._panels),
            directory=str(self._store.directory) if self._enabled else None,
            captures_started=self.captures_started,
            captures_completed=self.captures_completed,
            captures_failed=self.captures_failed,
            captures_cancelled=self.captures_cancelled,
            frames_captured=self.frames_captured,
            frames_selected=self.frames_selected,
            last_capture_at=(
                None
                if self.last_capture_at is None
                else self.last_capture_at.isoformat()
            ),
            last_error=self.last_error,
            barren_captures=self._barren,
            suspended=self._suspended,
        )


def _log_cadence(trigger_id: str, camera: CameraResult) -> None:
    """One line per body replay per shot: the rate asked for and the rate got."""

    cadence = camera.cadence
    if cadence is None or camera.kind is not PanelKind.DYNAMIC:
        return
    logger.info(
        "swing_capture_cadence",
        extra={
            "shot_trigger_id": trigger_id,
            "swing_capture_camera": camera.camera_id,
            "swing_capture_fps": cadence.requested_fps,
            "swing_capture_downswing_fps": cadence.downswing_fps,
            "swing_capture_achieved_fps": cadence.achieved_fps,
            "swing_capture_downswing_achieved_fps": cadence.downswing_achieved_fps,
            "swing_capture_planned_frames": cadence.planned,
            "swing_capture_frames": cadence.requested,
            "swing_capture_interval_mean_ms": cadence.mean_ms,
            "swing_capture_interval_median_ms": cadence.median_ms,
            "swing_capture_interval_min_ms": cadence.min_ms,
            "swing_capture_interval_max_ms": cadence.max_ms,
            "swing_capture_missed_deadlines": cadence.missed,
            "swing_capture_overruns": cadence.overruns,
            "swing_capture_request_mean_ms": cadence.request_mean_ms,
            "swing_capture_request_max_ms": cadence.request_max_ms,
            "swing_capture_spacing_holds": cadence.spacing_holds,
        },
    )


def _log_replay_start(trigger_id: str, camera: CameraResult) -> None:
    """Say when a body camera's clock is not its own, or there is no clock."""

    if camera.kind is not PanelKind.DYNAMIC or camera.view not in _BODY_VIEWS:
        return
    interval = camera.interval
    if interval is not None and interval.borrowed_from is not None:
        logger.warning(
            "swing_capture_replay_start_borrowed",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_camera": camera.camera_id,
                "swing_capture_source": camera.source,
                "swing_capture_interval_start": round(interval.start_seconds, 3),
                "swing_capture_detail": (
                    f"no replay start found on {camera.camera_id}; timed from "
                    f"{interval.borrowed_from}'s, because both body replays "
                    "begin on the same capture tick"
                ),
            },
        )
    elif camera.detail == _NO_REPLAY_START:
        logger.warning(
            "swing_capture_replay_start_not_found",
            extra={
                "shot_trigger_id": trigger_id,
                "swing_capture_camera": camera.camera_id,
                "swing_capture_source": camera.source,
                "swing_capture_frames": camera.captured,
                "swing_capture_detail": _NO_REPLAY_START,
            },
        )


def _usable_stills(panel: CapturePanel, frames: Sequence[CapturedFrame]) -> int:
    """How many kept stills this panel could honestly contribute."""

    distinct = distinct_count(frames)
    capacity = aim_capacity(panel.view) if panel.kind is PanelKind.DYNAMIC else None
    return distinct if capacity is None else min(distinct, capacity)


def _metrics_status(capture: SwingCapture) -> str:
    if capture.metrics is None:
        return "not_configured"
    return "captured" if capture.metrics.ok else "unavailable"


def _timeline(
    frames: Sequence[CapturedFrame], selected: frozenset[int]
) -> tuple[FrameTrace, ...]:
    traces: list[FrameTrace] = []
    previous: bytes | None = None
    for index, frame in enumerate(frames):
        size = jpeg_size(frame.data) or (0, 0)
        traces.append(
            FrameTrace(
                index=index + 1,
                offset_seconds=frame.offset_seconds,
                sha256=hashlib.sha256(frame.data).hexdigest()[:16],
                size_bytes=len(frame.data),
                width=size[0],
                height=size[1],
                changed=previous is None or frame.data != previous,
                selected=index in selected,
            )
        )
        previous = frame.data
    return tuple(traces)


def _changes(camera: CameraResult) -> tuple[float | None, float | None]:
    """First and last offset at which the picture changed, or None."""

    moments = [
        trace.offset_seconds
        for position, trace in enumerate(camera.timeline)
        if position > 0 and trace.changed
    ]
    if not moments:
        return None, None
    return round(moments[0], 3), round(moments[-1], 3)


def _frame_accounting(
    tracks: dict[str, list[CapturedFrame]],
) -> tuple[int, int, int]:
    """How many stills arrived, how many were blank, how many were repeats.

    A repeat is a still whose bytes match the previous still on the same
    panel. Blank means a tiny JPEG, which is how OBS reports a black frame.
    Neither count is decoded.
    """

    captured = 0
    blank = 0
    duplicate = 0
    for frames in tracks.values():
        previous: bytes | None = None
        for frame in frames:
            captured += 1
            if likely_blank(frame.data):
                blank += 1
            if previous is not None and frame.data == previous:
                duplicate += 1
            previous = frame.data
    return captured, blank, duplicate


def build_swing_capture_service(
    client_factory: ClientFactory,
    publish: Publish,
    *,
    enabled: bool,
    camera_sources: Sequence[str] = (),
    panels: Sequence[CapturePanel] | None = None,
    output_dir: Path | str,
    initial_delay_seconds: float,
    duration_seconds: float,
    fps: float,
    selected_frames: int,
    downswing_fps: float = 0.0,
    downswing_delay_seconds: float = 3.3,
    downswing_duration_seconds: float = 1.1,
    image_width: int = 0,
    static_delay_seconds: float = 3.5,
    static_duration_seconds: float = 1.5,
    static_fps: float = 2.0,
    debug_keep_shots: int = 0,
    debug_directory: Path | str | None = None,
    evidence_keep: int = EVIDENCE_KEEP,
    metrics_source: str = "",
    metrics_delay_seconds: float = 3.0,
    capture_scene: str = CAPTURE_SCENE,
    command_timeout: float = 10.0,
    shutdown_timeout: float = 5.0,
    announce_progress: bool = False,
    diagnostic_timeline_shots: int = 0,
) -> SwingCaptureService:
    """Assemble the subsystem from plain values, so tests need no `Settings`."""

    return SwingCaptureService(
        client_factory,
        camera_sources,
        LatestSwingStore(
            output_dir,
            debug_keep_shots=debug_keep_shots,
            debug_directory=debug_directory,
            evidence_keep=retention_limit(evidence_keep, diagnostic_timeline_shots),
        ),
        publish,
        enabled=enabled,
        initial_delay_seconds=initial_delay_seconds,
        duration_seconds=duration_seconds,
        fps=fps,
        downswing_fps=downswing_fps,
        downswing_delay_seconds=downswing_delay_seconds,
        downswing_duration_seconds=downswing_duration_seconds,
        selected_frames=selected_frames,
        # Zero means "whatever resolution the source is", which is what OBS
        # does when the field is absent. It is not a width of zero pixels.
        image_width=image_width or None,
        panels=panels,
        static_delay_seconds=static_delay_seconds,
        static_duration_seconds=static_duration_seconds,
        static_fps=static_fps,
        metrics_source=metrics_source,
        metrics_delay_seconds=metrics_delay_seconds,
        capture_scene=capture_scene,
        command_timeout=command_timeout,
        shutdown_timeout=shutdown_timeout,
        announce_progress=announce_progress,
        diagnostic_timeline_shots=diagnostic_timeline_shots,
    )
