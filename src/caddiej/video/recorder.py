"""One camera, recording continuously, restarting when it dies.

The recorder is the only thing that knows a camera can fail, and it is built so
that failing is ordinary. `serve` does not raise: a camera that cannot be opened,
an FFmpeg that exits, a device unplugged mid-round all end the same way — a state
change, a log line, a delay, and another attempt. After enough consecutive
failures it stops trying and says so, and that too is not an error.

This matters more than it sounds, because `TaskSupervisor` treats a supervised
task raising as a reason to shut CaddieJ down. That is right for the HUD server
and wrong for a webcam, so the containment lives here rather than in the caller.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from caddiej.domain.media import CaptureState
from caddiej.runtime.backoff import Backoff
from caddiej.video.cameras import CameraSpec
from caddiej.video.ffmpeg import CaptureProfile, Process, Spawn, capture_command, spawn
from caddiej.video.layout import MediaLayout
from caddiej.video.segments import (
    Gap,
    SegmentIndex,
    SegmentListReader,
    SegmentWindow,
)

logger = logging.getLogger(__name__)

StateObserver = Callable[[CaptureState, str | None], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]

# A capture that ran this long before dying counts as having worked, so the
# restart budget starts again. Otherwise a camera that drops out once an hour
# would exhaust its attempts over an evening and stay off.
_HEALTHY_RUN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CameraHealth:
    camera_id: str
    state: CaptureState
    segments_held: int
    segments_recorded: int
    segments_recycled: int
    buffer_seconds: float
    buffer_bytes: int
    restarts: int
    last_error: str | None


class CameraRecorder:
    """Keeps a rolling buffer of one camera on disk."""

    def __init__(
        self,
        camera: CameraSpec,
        layout: MediaLayout,
        profile: CaptureProfile | None = None,
        binary: str = "ffmpeg",
        spawn_process: Spawn = spawn,
        poll_interval: float = 0.25,
        restart_limit: int = 5,
        backoff: Backoff | None = None,
        stop_grace_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        on_state: StateObserver | None = None,
    ) -> None:
        self._camera = camera
        self._layout = layout
        self._profile = profile or CaptureProfile()
        self._binary = binary
        self._spawn = spawn_process
        self._poll_interval = poll_interval
        self._restart_limit = restart_limit
        self._backoff = backoff or Backoff(initial_seconds=0.5, maximum_seconds=10.0)
        self._stop_grace = stop_grace_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._on_state = on_state

        self._index = SegmentIndex(monotonic(), camera.latency_seconds)
        self._reader: SegmentListReader | None = None
        self._process: Process | None = None
        self._state = CaptureState.DISABLED

        self.segments_recorded = 0
        self.segments_recycled = 0
        self.restarts = 0
        self.last_error: str | None = None

    @property
    def camera(self) -> CameraSpec:
        return self._camera

    def observe(self, on_state: StateObserver) -> None:
        """Report state changes to whoever can publish them.

        Set after construction because the observer is usually the service that
        owns this recorder, and the two cannot both be built first.
        """

        self._on_state = on_state

    @property
    def state(self) -> CaptureState:
        return self._state

    @property
    def is_recording(self) -> bool:
        return self._state is CaptureState.RECORDING

    def health(self) -> CameraHealth:
        return CameraHealth(
            camera_id=self._camera.camera_id,
            state=self._state,
            segments_held=self._index.count,
            segments_recorded=self.segments_recorded,
            segments_recycled=self.segments_recycled,
            buffer_seconds=round(self._index.span_seconds, 2),
            buffer_bytes=self._index.total_bytes,
            restarts=self.restarts,
            last_error=self.last_error,
        )

    # --- what the clip worker asks -----------------------------------------

    def media_time(self, monotonic: float) -> float:
        return self._index.media_time(monotonic)

    def window(
        self, start_monotonic: float, end_monotonic: float
    ) -> SegmentWindow | Gap:
        """The footage covering a stretch of CaddieJ's own clock."""

        return self._index.window(
            self._index.media_time(start_monotonic),
            self._index.media_time(end_monotonic),
        )

    # --- the capture loop ---------------------------------------------------

    async def serve(self) -> None:
        """Record until cancelled. Never raises for a camera's sake."""

        failures = 0
        while True:
            started = self._monotonic()
            try:
                detail = await self._record()
            except asyncio.CancelledError:
                await self._set_state(CaptureState.DISABLED, "stopped")
                raise
            except Exception as error:
                detail = f"{type(error).__name__}: {error}"
                logger.error(
                    "camera_capture_failed",
                    extra={"camera_id": self._camera.camera_id},
                    exc_info=error,
                )

            self.last_error = detail
            if self._monotonic() - started >= _HEALTHY_RUN_SECONDS:
                failures = 0
                self._backoff.reset()
            failures += 1
            self.restarts += 1

            if failures > self._restart_limit:
                await self._set_state(CaptureState.UNAVAILABLE, detail)
                logger.error(
                    "camera_given_up",
                    extra={
                        "camera_id": self._camera.camera_id,
                        "video_detail": detail,
                        "video_restarts": self.restarts,
                    },
                )
                return

            delay = self._backoff.next_delay()
            await self._set_state(CaptureState.RETRYING, detail)
            logger.warning(
                "camera_restarting",
                extra={
                    "camera_id": self._camera.camera_id,
                    "video_detail": detail,
                    "video_backoff_seconds": round(delay, 2),
                },
            )
            await self._sleep(delay)

    async def _record(self) -> str:
        """One capture, from spawn to exit. Returns why it ended."""

        await self._set_state(CaptureState.STARTING)
        camera_id = self._camera.camera_id
        self._layout.prepare(camera_id)
        cleared = self._layout.clear_buffer(camera_id)
        self._index.reset(self._monotonic())
        self._reader = SegmentListReader(
            self._layout.segment_list(camera_id), self._layout.buffer_dir(camera_id)
        )

        args = capture_command(
            self._camera,
            self._profile,
            self._layout.segment_pattern(camera_id),
            self._layout.segment_list(camera_id),
            binary=self._binary,
        )
        process = await self._spawn(args)
        self._process = process
        logger.info(
            "camera_recording",
            extra={
                "camera_id": camera_id,
                "video_camera": self._camera.description,
                "video_recycled": cleared,
                "video_root": str(self._layout.root),
            },
        )
        await self._set_state(CaptureState.RECORDING)

        waiter = asyncio.create_task(process.wait(), name=f"ffmpeg-wait-{camera_id}")
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {waiter}, timeout=self._poll_interval
                )
                # Collected before checking the result, so the segments FFmpeg
                # wrote just before exiting are still picked up.
                self._collect()
                if done:
                    return f"ffmpeg exited with {waiter.result()}"
        finally:
            self._process = None
            if not waiter.done():
                with suppress(Exception):
                    await process.stop(self._stop_grace)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def _collect(self) -> None:
        """Take in new segments and drop the ones past the buffer's window.

        Synchronous file work on the event loop, deliberately: this is a short
        read of a text file and at most a handful of `unlink` calls per second.
        Handing that to a thread would cost more than it saves.
        """

        if self._reader is None:
            return
        for segment in self._reader.read(self._monotonic()):
            self._index.append(segment)
            self.segments_recorded += 1

        evicted = self._index.prune(
            self._profile.buffer_seconds, self._profile.buffer_max_bytes
        )
        for segment in evicted:
            _discard(segment.path)
            self.segments_recycled += 1

    async def aclose(self) -> None:
        """Stop the camera, leaving nothing running."""

        process = self._process
        self._process = None
        if process is not None:
            with suppress(Exception):
                await process.stop(self._stop_grace)
        await self._set_state(CaptureState.DISABLED, "stopped")

    async def _set_state(self, state: CaptureState, detail: str | None = None) -> None:
        if state is self._state:
            return
        self._state = state
        if self._on_state is not None:
            # Awaited rather than fired off: the observer publishes to a bus
            # whose queue is unbounded, so this cannot wait on anything, and a
            # detached task would be one more thing to shut down.
            with suppress(Exception):
                await self._on_state(state, detail)


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("video_segment_not_deleted", extra={"clip_path": str(path)})
