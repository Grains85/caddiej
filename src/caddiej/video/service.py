"""Filming swings, without a camera ever being able to delay a shot.

Three rules shape this file.

**Nothing on the event path touches a camera or a disk.** `on_shot_completed`
puts a request on a queue and returns; on the path where a clip is produced it
performs no awaits at all. The bus dispatches handlers one at a time, so a
handler that waited for FFmpeg would hold up the handler that clears the floor.

**A clip cannot be cut when it is asked for.** At the moment a shot arrives the
post-impact footage has not been recorded yet, so the worker's first job is to
wait for the future. Doing that anywhere but in the worker would be the one
mistake that makes filming block play.

**Video is never a reason to stop playing golf.** No FFmpeg, no camera, a device
unplugged mid-round, a full disk: each is reported through an event and through
`health()`, and none of them reaches the application.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.events import (
    CameraStateChanged,
    ShotCompleted,
    SwingClipFailed,
    SwingClipReady,
)
from caddiej.domain.media import CaptureState, ClipFailure, ClipRef
from caddiej.video.cameras import CameraSpec
from caddiej.video.clips import ClipExtractor, ClipRequest, new_clip_id
from caddiej.video.errors import ClipExtractionError
from caddiej.video.ffmpeg import CaptureProfile, is_available
from caddiej.video.frames import FrameExtractor
from caddiej.video.layout import MediaLayout
from caddiej.video.recorder import CameraRecorder
from caddiej.video.segments import Gap

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]

_STOP = object()
# How often the worker looks up while waiting for footage to exist.
_WAIT_SLICE = 0.1

_GAP_REASONS = {
    Gap.NOT_RECORDING: ClipFailure.NOT_RECORDING,
    Gap.RECYCLED: ClipFailure.BUFFER_RECYCLED,
    Gap.NOT_YET_RECORDED: ClipFailure.INCOMPLETE,
    Gap.INTERRUPTED: ClipFailure.INCOMPLETE,
}


@dataclass(frozen=True, slots=True)
class VideoHealth:
    """Everything that can go wrong with filming, in one place."""

    enabled: bool
    available: bool
    camera_id: str | None
    state: str
    media_root: str | None
    segments_held: int
    segments_recorded: int
    segments_recycled: int
    buffer_seconds: float
    buffer_bytes: int
    camera_restarts: int
    clips_requested: int
    clips_ready: int
    clips_failed: int
    clips_dropped: int
    clips_queued: int
    last_error: str | None


class VideoService:
    """Owns the camera, the clip queue, and the worker behind it."""

    def __init__(
        self,
        recorder: CameraRecorder,
        extractor: ClipExtractor,
        layout: MediaLayout,
        publish: Publish,
        frames: FrameExtractor | None = None,
        enabled: bool = True,
        pre_seconds: float = 2.0,
        post_seconds: float = 1.5,
        shot_report_lag_seconds: float = 0.0,
        settle_seconds: float = 1.5,
        queue_size: int = 8,
        drain_timeout_seconds: float = 5.0,
        binary: str = "ffmpeg",
        ffmpeg_available: Callable[[str], bool] = is_available,
        monotonic: Callable[[], float] = time.monotonic,
        now: Now = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._recorder = recorder
        self._extractor = extractor
        self._layout = layout
        self._publish = publish
        self._frames = frames
        self._enabled = enabled
        self._pre = pre_seconds
        self._post = post_seconds
        self._lag = shot_report_lag_seconds
        self._settle = settle_seconds
        self._drain_timeout = drain_timeout_seconds
        self._binary = binary
        self._ffmpeg_available = ffmpeg_available
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep

        self._queue: asyncio.Queue[ClipRequest | object] = asyncio.Queue(
            maxsize=queue_size
        )
        self._capture: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._available = False
        self._gave_up = False

        self.clips_requested = 0
        self.clips_ready = 0
        self.clips_failed = 0
        self.clips_dropped = 0
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def camera(self) -> CameraSpec:
        return self._recorder.camera

    @property
    def is_available(self) -> bool:
        return self._available

    async def open(self) -> None:
        """Start filming. Never raises.

        A missing FFmpeg is a supported way to run CaddieJ, not a failure to
        start it, so it disables video and says so.
        """

        if not self._enabled:
            logger.info("video_disabled")
            return

        if not self._ffmpeg_available(self._binary):
            self.last_error = f"{self._binary} was not found"
            logger.error("video_unavailable", extra={"video_detail": self.last_error})
            return

        self._available = True
        # Stills from a run that did not finish cleanly. Harmless, but they are
        # frames of somebody's swing and nothing is going to read them.
        self._layout.clear_frames()
        self._capture = asyncio.create_task(
            self._film(), name=f"video-capture-{self.camera.camera_id}"
        )
        self._worker = asyncio.create_task(self._work(), name="video-clips")
        logger.info(
            "video_started",
            extra={
                "camera_id": self.camera.camera_id,
                "video_camera": self.camera.description,
                "video_root": str(self._layout.root),
            },
        )

    async def aclose(self) -> None:
        """Finish what is in the queue, stop the camera, leave nothing running."""

        self._available = False
        self._gave_up = False
        if self._worker is None and self._capture is None:
            # Never started: switched off, or no FFmpeg to start.
            return

        if self._worker is not None:
            await self._queue.put(_STOP)
            try:
                await asyncio.wait_for(self._worker, self._drain_timeout)
            except TimeoutError:
                # A pending clip is waiting for footage that shutdown means it
                # will never get. Shutdown is not negotiable.
                logger.warning(
                    "video_drain_timed_out",
                    extra={"video_queued": self._queue.qsize()},
                )
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

        if self._capture is not None:
            self._capture.cancel()
            await asyncio.gather(self._capture, return_exceptions=True)
            self._capture = None
        await self._recorder.aclose()
        logger.info("video_stopped", extra={"camera_id": self.camera.camera_id})

    async def _film(self) -> None:
        """Run the recorder, containing anything it manages to raise.

        The recorder already survives camera failures; this is the backstop that
        keeps a bug in it from reaching the task supervisor, which would read it
        as a reason to shut CaddieJ down.
        """

        try:
            await self._recorder.serve()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.error(
                "video_capture_abandoned",
                extra={"camera_id": self.camera.camera_id},
                exc_info=error,
            )

        # `serve` returning at all means the camera has been given up on: it
        # retries by itself until it runs out of patience. So video is no longer
        # available, and saying otherwise would have every later shot queue for
        # a clip that cannot exist and fail one at a time — which reads, to
        # anybody looking at health, as a clipping problem rather than as the
        # camera that has been unplugged for an hour.
        if self._available:
            self._available = False
            self._gave_up = True
            logger.error(
                "video_unavailable",
                extra={
                    "camera_id": self.camera.camera_id,
                    "video_detail": "the camera was given up on",
                    "video_restarts": self._recorder.restarts,
                },
            )

    # --- the event path ----------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(ShotCompleted, self.on_shot_completed, name="video")

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Ask for a clip of this swing. Awaits nothing when it succeeds."""

        if not self._available:
            # Video being switched off, or having no FFmpeg to run, means clips
            # are not part of this session and nobody is waiting for one. A
            # camera that was filming and has since been given up on is the
            # opposite: it has to be answered, or a coaching job holding this
            # shot open for footage holds it open for good.
            if self._gave_up:
                await self._refuse(event.shot.shot_id)
            return

        source = event.shot.source
        self.clips_requested += 1
        # The launch monitor reports a shot some time after the ball was struck,
        # so the moment worth filming is earlier than the moment CaddieJ heard
        # about it. How much earlier is device-specific and configured.
        request = ClipRequest(
            clip_id=new_clip_id(),
            shot_id=event.shot.shot_id,
            camera_id=self.camera.camera_id,
            impact_monotonic=source.observed_monotonic - self._lag,
            impact_at=source.observed_at - timedelta(seconds=self._lag),
            pre_seconds=self._pre,
            post_seconds=self._post,
        )
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            self.clips_dropped += 1
            logger.error(
                "video_clip_dropped",
                extra={
                    "shot_id": str(request.shot_id),
                    "video_dropped": self.clips_dropped,
                },
            )
            # Only reached when clips are already backed up, and the bus queue
            # is unbounded, so this cannot wait on anything.
            await self._publish(
                SwingClipFailed(
                    shot_id=request.shot_id,
                    camera_id=request.camera_id,
                    reason=ClipFailure.QUEUE_FULL,
                    detail="clip requests were already backed up",
                )
            )

    async def _refuse(self, shot_id: UUID) -> None:
        """Say straight away that there is no camera to film this swing.

        Not queued: the queue exists to keep FFmpeg off the event path, and
        there is no FFmpeg to run. Counted as requested and failed so health
        still shows how many swings went unfilmed.
        """

        self.clips_requested += 1
        self.clips_failed += 1
        await self._publish(
            SwingClipFailed(
                shot_id=shot_id,
                camera_id=self.camera.camera_id,
                reason=ClipFailure.NOT_RECORDING,
                detail="the camera was given up on",
            )
        )

    # --- the worker --------------------------------------------------------

    async def _work(self) -> None:
        """Take clip requests one at a time until told to stop. Must not raise."""

        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            if not isinstance(item, ClipRequest):
                continue
            try:
                await self._fulfil(item)
            except asyncio.CancelledError:
                # Shutdown, part way through a clip. Said rather than dropped:
                # a coaching job may be holding this shot open waiting for
                # footage, and the bus is still running for a moment yet.
                await self._fail(
                    item, ClipFailure.ABANDONED, "CaddieJ stopped before it was cut"
                )
                raise
            except Exception as error:
                await self._fail(item, ClipFailure.EXTRACTION_FAILED, error)

    async def _fulfil(self, request: ClipRequest) -> None:
        await self._wait_for_footage(request)

        outcome = self._recorder.window(request.start_monotonic, request.end_monotonic)
        if isinstance(outcome, Gap):
            await self._fail(request, _GAP_REASONS[outcome], outcome.value)
            return

        try:
            reference = await self._extractor.extract(
                request,
                outcome,
                self._recorder.media_time(request.impact_monotonic),
                self.camera,
                recorded_at=self._now(),
            )
        except TimeoutError as error:
            await self._fail(request, ClipFailure.TIMED_OUT, error)
            return
        except ClipExtractionError as error:
            await self._fail(request, ClipFailure.EXTRACTION_FAILED, error)
            return
        except OSError as error:
            await self._fail(request, ClipFailure.EXTRACTION_FAILED, error)
            return

        self.clips_ready += 1
        await self._publish(SwingClipReady(shot_id=request.shot_id, clip=reference))

    async def _wait_for_footage(self, request: ClipRequest) -> None:
        """Sleep until the end of the window has been recorded.

        The settle allowance covers the segment holding the last frames being
        closed and appearing in FFmpeg's index; asking before that would find a
        window that is merely not recorded yet.
        """

        deadline = request.end_monotonic + self._settle
        while (remaining := deadline - self._monotonic()) > 0:
            await self._sleep(min(remaining, _WAIT_SLICE))

    async def _fail(
        self, request: ClipRequest, reason: ClipFailure, detail: object
    ) -> None:
        self.clips_failed += 1
        self.last_error = f"{reason.value}: {detail}"
        logger.error(
            "video_clip_failed",
            extra={
                "shot_id": str(request.shot_id),
                "clip_id": str(request.clip_id),
                "camera_id": request.camera_id,
                "clip_reason": reason.value,
                "video_detail": str(detail)[:400],
            },
        )
        await self._publish(
            SwingClipFailed(
                shot_id=request.shot_id,
                camera_id=request.camera_id,
                reason=reason,
                detail=str(detail)[:400],
            )
        )

    # --- looking at what was filmed ----------------------------------------

    async def frames(
        self, clip: ClipRef, offsets: Sequence[float]
    ) -> tuple[bytes, ...]:
        """Stills from a finished clip, at those offsets into it. Never raises.

        The subsystem's whole outward-facing answer to "let me see the swing".
        Deliberately bytes rather than a path or a decoder handle: a caller that
        held either would be holding a piece of the video subsystem, and this is
        the seam that keeps FFmpeg from spreading.
        """

        if self._frames is None or not self._available:
            return ()
        return await self._frames.stills(clip, offsets)

    # --- reporting ---------------------------------------------------------

    async def on_camera_state(self, state: CaptureState, detail: str | None) -> None:
        """Passed to the recorder so a camera change reaches the bus."""

        await self._publish(
            CameraStateChanged(
                camera_id=self.camera.camera_id, state=state, detail=detail
            )
        )

    def health(self) -> VideoHealth:
        camera = self._recorder.health()
        return VideoHealth(
            enabled=self._enabled,
            available=self._available,
            camera_id=camera.camera_id if self._enabled else None,
            state=camera.state.value,
            media_root=str(self._layout.root) if self._enabled else None,
            segments_held=camera.segments_held,
            segments_recorded=camera.segments_recorded,
            segments_recycled=camera.segments_recycled,
            buffer_seconds=camera.buffer_seconds,
            buffer_bytes=camera.buffer_bytes,
            camera_restarts=camera.restarts,
            clips_requested=self.clips_requested,
            clips_ready=self.clips_ready,
            clips_failed=self.clips_failed,
            clips_dropped=self.clips_dropped,
            clips_queued=self._queue.qsize(),
            last_error=self.last_error or camera.last_error,
        )


def build_video_service(
    enabled: bool,
    media_root: Path | str,
    camera: CameraSpec,
    publish: Publish,
    profile: CaptureProfile | None = None,
    binary: str = "ffmpeg",
    pre_seconds: float = 2.0,
    post_seconds: float = 1.5,
    shot_report_lag_seconds: float = 0.0,
    queue_size: int = 8,
    restart_limit: int = 5,
    extract_timeout_seconds: float = 30.0,
    drain_timeout_seconds: float = 5.0,
    frame_width: int = 512,
) -> VideoService:
    """Assemble the subsystem from plain values, so tests need no `Settings`."""

    resolved = profile or CaptureProfile()
    layout = MediaLayout(media_root)
    extractor = ClipExtractor(
        layout, binary=binary, timeout_seconds=extract_timeout_seconds
    )
    frames = FrameExtractor(layout, binary=binary, width=frame_width)
    recorder = CameraRecorder(
        camera,
        layout,
        resolved,
        binary=binary,
        restart_limit=restart_limit,
    )
    service = VideoService(
        recorder,
        extractor,
        layout,
        publish,
        frames=frames,
        enabled=enabled,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        shot_report_lag_seconds=shot_report_lag_seconds,
        # Long enough for the segment holding the last frames to be closed and
        # reported, which takes one segment plus a poll.
        settle_seconds=resolved.segment_seconds * 2,
        queue_size=queue_size,
        drain_timeout_seconds=drain_timeout_seconds,
        binary=binary,
    )
    # The recorder cannot publish; it reports to the service, which can.
    recorder.observe(service.on_camera_state)
    return service
