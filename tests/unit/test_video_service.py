"""The service, which is where filming is kept away from playing golf.

The tests that matter most here are the ones about what does *not* happen: a
handler that does not wait, a camera failure that does not propagate, a missing
FFmpeg that does not stop startup. A golf simulator that pauses because a webcam
is unhappy is worse than one with no cameras at all.

No FFmpeg runs in this file and no time passes. The camera is a fake process, the
buffer is files a test wrote, and the clock is held still, so a two-second window
around a swing that happened four seconds ago is arithmetic rather than a wait.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.conftest import ShotBuilder

from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.events import (
    CameraStateChanged,
    ShotCompleted,
    SwingClipFailed,
    SwingClipReady,
)
from caddiej.domain.media import CaptureState, ClipFailure
from caddiej.runtime.backoff import Backoff
from caddiej.video.cameras import synthetic_camera
from caddiej.video.clips import ClipExtractor
from caddiej.video.fakes import FakeRun, FakeSpawn, write_segments
from caddiej.video.ffmpeg import CaptureProfile
from caddiej.video.layout import MediaLayout
from caddiej.video.recorder import CameraRecorder
from caddiej.video.service import VideoService

CAMERA = synthetic_camera(camera_id="tee", fps=30)


class Published:
    """Everything the service announced."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of[EventType: Event](self, kind: type[EventType]) -> list[EventType]:
        return [event for event in self.events if isinstance(event, kind)]


class Clock:
    """A monotonic reading that does not move unless a test moves it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def until(condition: Callable[[], object], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError("it never happened")
        await asyncio.sleep(0.005)


class Harness:
    """A whole video subsystem with nothing external behind it."""

    def __init__(
        self,
        tmp_path: Path,
        run: FakeRun | None = None,
        queue_size: int = 8,
        enabled: bool = True,
        ffmpeg_present: bool = True,
        lag: float = 0.0,
        drain_timeout: float = 5.0,
        restart_limit: int = 5,
    ) -> None:
        self.clock = Clock()
        self.published = Published()
        self.layout = MediaLayout(tmp_path)
        self.spawn = FakeSpawn()
        self.run = run or FakeRun()
        self.recorder = CameraRecorder(
            CAMERA,
            self.layout,
            CaptureProfile(buffer_seconds=30.0),
            spawn_process=self.spawn,
            poll_interval=0.01,
            restart_limit=restart_limit,
            backoff=Backoff(initial_seconds=0.001, maximum_seconds=0.002),
            monotonic=self.clock,
        )
        self.service = VideoService(
            self.recorder,
            ClipExtractor(self.layout, run_command=self.run),
            self.layout,
            self.published,
            enabled=enabled,
            pre_seconds=2.0,
            post_seconds=1.5,
            shot_report_lag_seconds=lag,
            # The footage these tests use already exists, so there is nothing to
            # wait for.
            settle_seconds=0.0,
            queue_size=queue_size,
            drain_timeout_seconds=drain_timeout,
            ffmpeg_available=lambda _binary: ffmpeg_present,
            monotonic=self.clock,
        )
        self.recorder.observe(self.service.on_camera_state)

    async def __aenter__(self) -> "Harness":
        await self.service.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.service.aclose()

    async def filming(self) -> None:
        await until(lambda: self.recorder.state is CaptureState.RECORDING)

    async def recording(self, segments: int = 10) -> None:
        """Film, and put `segments` seconds of footage in the buffer."""

        await self.filming()
        write_segments(self.layout.buffer_dir("tee"), count=segments)
        await until(lambda: self.recorder.health().segments_held == segments)

    def swing(self, make_shot: ShotBuilder, ago: float = 2.0) -> ShotCompleted:
        return ShotCompleted(shot=make_shot(observed_monotonic=self.clock.now - ago))


@pytest.fixture
def harness(tmp_path: Path) -> Callable[..., Harness]:
    def build(**kwargs: object) -> Harness:
        return Harness(tmp_path, **kwargs)  # type: ignore[arg-type]

    return build


# --- the happy path --------------------------------------------------------


async def test_a_shot_produces_a_clip(harness, make_shot: ShotBuilder) -> None:
    async with harness() as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipReady))

        ready = run.published.of(SwingClipReady)[0]
        assert (run.layout.root / ready.clip.path).exists()
        assert run.service.health().clips_ready == 1


async def test_a_clip_is_announced_only_once_it_is_on_disk(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness() as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipReady))

        clip = run.layout.root / run.published.of(SwingClipReady)[0].clip.path
        assert clip.stat().st_size > 0
        assert clip.with_suffix(".json").exists()


async def test_the_clip_covers_the_configured_window(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness() as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipReady))

        clip = run.published.of(SwingClipReady)[0].clip
        # Two seconds before impact and one and a half after, widened to whole
        # segments, so at least the requested three and a half.
        assert clip.duration_seconds >= 3.5
        assert clip.impact_offset_seconds >= 2.0
        assert clip.shot_id == run.published.of(SwingClipReady)[0].shot_id


async def test_a_reporting_delay_puts_impact_earlier_than_the_message(
    harness, make_shot: ShotBuilder
) -> None:
    # A launch monitor that takes a second to report a shot would otherwise
    # centre every clip a second late, on the golfer walking away.
    async with harness(lag=1.0) as run:
        await run.recording()
        event = run.swing(make_shot, ago=4.0)

        await run.service.on_shot_completed(event)
        await until(lambda: run.published.of(SwingClipReady))

        clip = run.published.of(SwingClipReady)[0].clip
        assert (event.shot.source.observed_at - clip.impact_at).total_seconds() == 1.0


# --- staying off the event path --------------------------------------------


async def test_a_shot_handler_does_not_wait_for_a_clip(
    harness, make_shot: ShotBuilder
) -> None:
    # The bus dispatches handlers one at a time. A handler that waited for
    # FFmpeg would hold up the handler that clears the floor.
    async with harness(run=FakeRun(delay=30.0), drain_timeout=0.05) as run:
        await run.recording()

        await asyncio.wait_for(
            run.service.on_shot_completed(run.swing(make_shot, ago=4.0)), timeout=0.2
        )

        assert run.service.health().clips_requested == 1


async def test_a_backlog_refuses_new_clips_rather_than_growing(
    harness, make_shot: ShotBuilder
) -> None:
    # Reaching this needs a wedged disk, and the alternative to refusing is a
    # growing queue of requests for footage that has already been recycled.
    async with harness(
        run=FakeRun(delay=30.0), queue_size=1, drain_timeout=0.05
    ) as run:
        await run.recording()

        for _ in range(4):
            await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))

        failures = run.published.of(SwingClipFailed)
        assert failures, "a refused clip has to be reported"
        assert failures[0].reason is ClipFailure.QUEUE_FULL
        assert run.service.health().clips_dropped >= 1


# --- when there is nothing to clip -----------------------------------------


async def test_a_shot_with_nothing_recorded_is_reported(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness() as run:
        await run.filming()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipFailed))

        assert run.published.of(SwingClipFailed)[0].reason is ClipFailure.NOT_RECORDING


async def test_a_swing_already_recycled_is_reported_as_such(
    harness, make_shot: ShotBuilder
) -> None:
    # Different from never having filmed it, and the difference is what tells
    # somebody their buffer is too short.
    async with harness() as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=600.0))
        await until(lambda: run.published.of(SwingClipFailed))

        assert (
            run.published.of(SwingClipFailed)[0].reason is ClipFailure.BUFFER_RECYCLED
        )


async def test_a_failing_extraction_is_reported_and_survived(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness(run=FakeRun(returncode=1, stderr="moov atom not found")) as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipFailed))

        failed = run.published.of(SwingClipFailed)[0]
        assert failed.reason is ClipFailure.EXTRACTION_FAILED
        assert "moov atom" in (failed.detail or "")
        assert run.service.health().clips_failed == 1


async def test_a_failed_clip_does_not_stop_the_next_one(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness(run=FakeRun(returncode=1, stderr="broken")) as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipFailed))

        run.run.returncode = 0
        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipReady))

        assert run.service.health().clips_ready == 1


async def test_an_extraction_that_hangs_is_given_up_on(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness(run=FakeRun(raises=TimeoutError("took too long"))) as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipFailed))

        assert run.published.of(SwingClipFailed)[0].reason is ClipFailure.TIMED_OUT


async def test_the_worker_survives_something_nobody_predicted(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness(run=FakeRun(raises=ZeroDivisionError("impossible"))) as run:
        await run.recording()

        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipFailed))

        run.run.raises = None
        await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))
        await until(lambda: run.published.of(SwingClipReady))


# --- being switched off ----------------------------------------------------


async def test_video_switched_off_films_nothing(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness(enabled=False) as run:
        await run.service.on_shot_completed(run.swing(make_shot))
        await asyncio.sleep(0)

        assert run.spawn.commands == []
        assert run.published.events == []
        assert not (run.layout.root / "buffer").exists()
        assert run.service.health().enabled is False
        assert run.service.health().camera_id is None


async def test_a_media_root_that_is_not_a_directory_does_not_stop_startup(
    tmp_path: Path,
) -> None:
    """`open` promises not to raise, and it runs before the bus, history and the
    HUD have anything to stop them — so a raise here takes the whole of start-up
    with nothing cleaning up behind it."""

    blocked = tmp_path / "in-the-way"
    blocked.write_text("a file where a directory should be", encoding="utf-8")
    run = Harness(blocked)

    await run.service.open()
    await run.service.aclose()


async def test_a_missing_ffmpeg_disables_video_rather_than_startup(
    harness, make_shot: ShotBuilder
) -> None:
    # No FFmpeg is a supported way to run CaddieJ, not a failure to start it.
    async with harness(ffmpeg_present=False) as run:
        assert run.service.is_available is False
        assert "not found" in (run.service.health().last_error or "")

        await run.service.on_shot_completed(run.swing(make_shot))
        await asyncio.sleep(0)

        assert run.spawn.commands == []
        assert run.published.of(SwingClipReady) == []
        assert run.service.health().clips_requested == 0


# --- reporting -------------------------------------------------------------


async def test_a_camera_change_reaches_the_bus(harness) -> None:
    async with harness() as run:
        await until(lambda: len(run.published.of(CameraStateChanged)) >= 2)

        changes = run.published.of(CameraStateChanged)
        assert [event.state for event in changes[:2]] == [
            CaptureState.STARTING,
            CaptureState.RECORDING,
        ]
        assert changes[0].camera_id == "tee"


async def test_health_reports_the_buffer_and_the_clips(
    harness, make_shot: ShotBuilder
) -> None:
    async with harness() as run:
        await run.recording(segments=5)
        await run.service.on_shot_completed(run.swing(make_shot, ago=3.0))
        await until(lambda: run.published.of(SwingClipReady))

        health = run.service.health()
        assert (health.enabled, health.available) == (True, True)
        assert health.state == "recording"
        assert health.camera_id == "tee"
        assert health.media_root is not None
        assert health.segments_held == 5
        assert health.buffer_seconds == pytest.approx(5.0)
        assert health.buffer_bytes > 0
        assert (health.clips_requested, health.clips_ready) == (1, 1)
        assert (health.clips_failed, health.clips_dropped) == (0, 0)


async def test_a_shot_on_the_bus_is_enough_to_get_a_clip(
    harness, make_shot: ShotBuilder
) -> None:
    bus = AsyncEventBus()
    await bus.start()
    try:
        async with harness() as run:
            run.service.subscribe(bus)
            await run.recording()

            await bus.publish(run.swing(make_shot, ago=4.0))
            await until(lambda: run.published.of(SwingClipReady))
    finally:
        await bus.stop()


# --- shutting down ---------------------------------------------------------


async def test_closing_stops_the_camera(harness) -> None:
    run = harness()
    await run.service.open()
    await run.filming()
    process = run.spawn.latest

    await run.service.aclose()

    assert process.stopped is True
    assert run.recorder.state is CaptureState.DISABLED


async def test_a_queued_clip_is_finished_before_shutdown_completes(
    harness, make_shot: ShotBuilder
) -> None:
    run = harness()
    await run.service.open()
    await run.recording()
    await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))

    await run.service.aclose()

    assert run.service.health().clips_ready == 1


async def test_shutdown_does_not_wait_forever_for_a_clip(
    harness, make_shot: ShotBuilder
) -> None:
    # Shutdown is not negotiable, and a clip whose footage has not been recorded
    # yet will never get it once CaddieJ is stopping.
    run = harness(run=FakeRun(delay=30.0), drain_timeout=0.05)
    await run.service.open()
    await run.recording()
    await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))

    await asyncio.wait_for(run.service.aclose(), timeout=2.0)


async def test_a_clip_abandoned_by_shutdown_is_said_rather_than_dropped(
    harness, make_shot: ShotBuilder
) -> None:
    """Something may be waiting on the clip. Coaching holds a shot open for
    footage, so a clip that silently never arrives is a job that waits out its
    whole allowance for a file nobody is writing any more."""

    run = harness(run=FakeRun(delay=30.0), drain_timeout=0.05)
    await run.service.open()
    await run.recording()
    swing = run.swing(make_shot, ago=4.0)
    await run.service.on_shot_completed(swing)
    await until(lambda: run.run.commands)

    await asyncio.wait_for(run.service.aclose(), timeout=2.0)

    failed = run.published.of(SwingClipFailed)
    assert [event.reason for event in failed] == [ClipFailure.ABANDONED]
    assert failed[0].shot_id == swing.shot.shot_id


async def test_a_camera_given_up_on_leaves_video_unavailable(
    harness, make_shot: ShotBuilder
) -> None:
    """The recorder retries by itself and eventually stops. Continuing to report
    video as available would have every later shot queue for a clip that cannot
    exist and fail one at a time, which reads as a clipping problem rather than
    as the camera that has been unplugged for an hour."""

    run = harness(restart_limit=0)
    await run.service.open()
    await run.filming()

    # A camera that has gone: the recording ends, the restart cannot start one,
    # and with no patience left `serve` returns.
    run.spawn.fail_with = OSError("no such device")
    run.spawn.latest.end(exit_code=1)
    await until(lambda: not run.service.is_available, timeout=2.0)

    assert run.service.health().available is False
    assert run.service.health().state == CaptureState.UNAVAILABLE.value

    await run.service.aclose()


async def test_a_swing_with_no_camera_left_is_answered_not_dropped(
    harness, make_shot: ShotBuilder
) -> None:
    """Video being unavailable is not permission to stay silent. Coaching holds
    a shot open waiting for footage, so a swing that gets no clip and no failure
    is a job that waits out its whole allowance for a file nobody is writing."""

    run = harness(restart_limit=0)
    await run.service.open()
    await run.filming()
    run.spawn.fail_with = OSError("no such device")
    run.spawn.latest.end(exit_code=1)
    await until(lambda: not run.service.is_available, timeout=2.0)

    swing = run.swing(make_shot, ago=4.0)
    await run.service.on_shot_completed(swing)

    failed = run.published.of(SwingClipFailed)
    assert [event.reason for event in failed] == [ClipFailure.NOT_RECORDING]
    assert failed[0].shot_id == swing.shot.shot_id
    # Counted, so health still shows how many swings went unfilmed, and no clip
    # was queued for a camera that is not there.
    assert run.service.health().clips_requested == 1
    assert run.service.health().clips_failed == 1
    assert run.service.health().clips_queued == 0

    await run.service.aclose()


async def test_a_shot_after_shutdown_is_ignored(
    harness, make_shot: ShotBuilder
) -> None:
    run = harness()
    await run.service.open()
    await run.recording()
    await run.service.aclose()

    await run.service.on_shot_completed(run.swing(make_shot, ago=4.0))

    assert run.service.health().clips_requested == 0
