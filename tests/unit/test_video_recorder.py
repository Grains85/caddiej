"""The recorder, with FFmpeg replaced by a fake that never runs.

The behaviour under test is almost entirely about failure: a camera that will not
open, an FFmpeg that exits, a device unplugged mid-round. All of them have to end
in a state change and another attempt, and none of them may reach the caller —
because the task supervisor reads a supervised task raising as a reason to shut
CaddieJ down, which is right for the HUD server and wrong for a webcam.
"""

import asyncio
from pathlib import Path

import pytest

from caddiej.domain.media import CaptureState
from caddiej.runtime.backoff import Backoff
from caddiej.video.cameras import synthetic_camera
from caddiej.video.fakes import FakeSpawn, write_segments
from caddiej.video.ffmpeg import CaptureProfile
from caddiej.video.layout import MediaLayout
from caddiej.video.recorder import CameraRecorder
from caddiej.video.segments import Gap, SegmentWindow

CAMERA = synthetic_camera(camera_id="tee", fps=30)


class Clock:
    """A monotonic reading a test can move by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Recorded:
    """Every state the camera reported, in order."""

    def __init__(self) -> None:
        self.states: list[tuple[CaptureState, str | None]] = []

    async def __call__(self, state: CaptureState, detail: str | None) -> None:
        self.states.append((state, detail))

    @property
    def sequence(self) -> list[CaptureState]:
        return [state for state, _detail in self.states]


def build(
    tmp_path: Path,
    spawn: FakeSpawn,
    clock: Clock | None = None,
    observer: Recorded | None = None,
    restart_limit: int = 5,
    profile: CaptureProfile | None = None,
) -> CameraRecorder:
    return CameraRecorder(
        CAMERA,
        MediaLayout(tmp_path),
        profile or CaptureProfile(buffer_seconds=3.0),
        spawn_process=spawn,
        poll_interval=0.01,
        restart_limit=restart_limit,
        # Zero jitter and a tiny delay: a test about restarting must not be a
        # test about waiting.
        backoff=Backoff(
            initial_seconds=0.001, maximum_seconds=0.001, jitter=lambda: 1.0
        ),
        monotonic=clock or Clock(),
        on_state=observer,
    )


async def until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError("the recorder never got there")
        await asyncio.sleep(0.005)


async def serving(recorder: CameraRecorder) -> asyncio.Task[None]:
    task = asyncio.create_task(recorder.serve())
    await until(lambda: recorder.state is CaptureState.RECORDING)
    return task


async def stop(task: asyncio.Task[None], recorder: CameraRecorder) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await recorder.aclose()


# --- recording -------------------------------------------------------------


async def test_recording_starts_the_camera_once(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    recorder = build(tmp_path, spawn)

    task = await serving(recorder)

    assert len(spawn.commands) == 1
    assert spawn.commands[0][0] == "ffmpeg"
    await stop(task, recorder)


async def test_a_camera_reports_starting_then_recording(tmp_path: Path) -> None:
    observer = Recorded()
    recorder = build(tmp_path, FakeSpawn(), observer=observer)

    task = await serving(recorder)

    assert observer.sequence[:2] == [CaptureState.STARTING, CaptureState.RECORDING]
    await stop(task, recorder)


async def test_segments_ffmpeg_writes_are_taken_into_the_buffer(
    tmp_path: Path,
) -> None:
    recorder = build(tmp_path, FakeSpawn())
    task = await serving(recorder)

    write_segments(MediaLayout(tmp_path).buffer_dir("tee"), count=3)
    await until(lambda: recorder.health().segments_held == 3)

    assert recorder.health().segments_recorded == 3
    await stop(task, recorder)


async def test_footage_past_the_buffer_window_is_deleted(tmp_path: Path) -> None:
    recorder = build(tmp_path, FakeSpawn())
    task = await serving(recorder)
    directory = MediaLayout(tmp_path).buffer_dir("tee")

    paths = write_segments(directory, count=8)
    await until(lambda: recorder.health().segments_recycled >= 4)

    # The buffer holds three seconds, so the earliest files are gone from the
    # index and from the disk.
    assert not paths[0].exists()
    assert paths[-1].exists()
    assert recorder.health().buffer_seconds <= 4.0
    await stop(task, recorder)


async def test_a_restart_throws_away_footage_it_cannot_place(tmp_path: Path) -> None:
    # FFmpeg's clock and its segment counter both restart at zero, so keeping
    # the old files would mean serving frames from the wrong minute.
    directory = MediaLayout(tmp_path).buffer_dir("tee")
    stale = write_segments(directory, count=3)
    recorder = build(tmp_path, FakeSpawn())

    task = await serving(recorder)

    assert not any(path.exists() for path in stale)
    assert recorder.health().segments_held == 0
    await stop(task, recorder)


# --- finding a swing -------------------------------------------------------


async def test_the_recorder_answers_which_files_a_swing_is_in(
    tmp_path: Path,
) -> None:
    clock = Clock(1000.0)
    recorder = build(
        tmp_path, FakeSpawn(), clock=clock, profile=CaptureProfile(buffer_seconds=30.0)
    )
    task = await serving(recorder)

    write_segments(MediaLayout(tmp_path).buffer_dir("tee"), count=6)
    await until(lambda: recorder.health().segments_held == 6)

    # Six seconds of footage exist and the last segment closed at "now", so a
    # swing between 3.2 and 1.1 seconds ago is media time 2.8 to 4.9.
    window = recorder.window(clock.now - 3.2, clock.now - 1.1)

    assert isinstance(window, SegmentWindow)
    assert [segment.sequence for segment in window.segments] == [2, 3, 4]
    # And the clip holds a little more than was asked for, because it starts
    # where a segment starts.
    assert (window.media_start, window.media_end) == (2.0, 5.0)
    await stop(task, recorder)


async def test_a_swing_with_no_footage_is_reported_as_such(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    recorder = build(tmp_path, FakeSpawn(), clock=clock)
    task = await serving(recorder)

    assert recorder.window(clock.now - 2.0, clock.now) is Gap.NOT_RECORDING
    await stop(task, recorder)


# --- when the camera fails -------------------------------------------------


async def test_a_camera_that_stops_is_started_again(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    observer = Recorded()
    recorder = build(tmp_path, spawn, observer=observer)
    task = await serving(recorder)

    spawn.latest.end(exit_code=1)
    await until(lambda: len(spawn.commands) == 2)

    assert CaptureState.RETRYING in observer.sequence
    assert recorder.health().restarts >= 1
    assert "ffmpeg exited with 1" in (recorder.last_error or "")
    await stop(task, recorder)


async def test_a_camera_that_will_not_open_is_retried(tmp_path: Path) -> None:
    spawn = FakeSpawn(fail_with=FileNotFoundError("no such device"))
    recorder = build(tmp_path, spawn, restart_limit=2)

    # Never raises: serve returns when it gives up.
    await asyncio.wait_for(recorder.serve(), timeout=2.0)

    assert len(spawn.commands) == 3
    assert recorder.state is CaptureState.UNAVAILABLE
    assert "FileNotFoundError" in (recorder.last_error or "")


async def test_giving_up_is_reported_rather_than_raised(tmp_path: Path) -> None:
    # A dead camera must not reach the task supervisor, which would read it as a
    # reason to shut CaddieJ down.
    observer = Recorded()
    recorder = build(
        tmp_path,
        FakeSpawn(fail_with=OSError("device is busy")),
        observer=observer,
        restart_limit=0,
    )

    await asyncio.wait_for(recorder.serve(), timeout=2.0)

    assert observer.sequence[-1] is CaptureState.UNAVAILABLE
    assert observer.states[-1][1] is not None


async def test_a_camera_keeps_being_retried_up_to_its_budget(
    tmp_path: Path,
) -> None:
    spawn = FakeSpawn(fail_with=OSError("busy"))
    recorder = build(tmp_path, spawn, restart_limit=4)

    await asyncio.wait_for(recorder.serve(), timeout=2.0)

    assert len(spawn.commands) == 5


async def test_an_observer_that_fails_does_not_stop_the_camera(
    tmp_path: Path,
) -> None:
    async def explode(_state: CaptureState, _detail: str | None) -> None:
        raise RuntimeError("the bus is on fire")

    recorder = build(tmp_path, FakeSpawn())
    recorder.observe(explode)

    task = asyncio.create_task(recorder.serve())
    await until(lambda: recorder.state is CaptureState.RECORDING)

    await stop(task, recorder)


# --- shutting down ---------------------------------------------------------


async def test_closing_stops_the_camera(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    recorder = build(tmp_path, spawn)
    task = await serving(recorder)
    process = spawn.latest

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await recorder.aclose()

    assert process.stopped is True
    assert recorder.state is CaptureState.DISABLED


async def test_cancelling_leaves_no_camera_running(tmp_path: Path) -> None:
    spawn = FakeSpawn()
    recorder = build(tmp_path, spawn)
    task = await serving(recorder)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawn.latest.stopped is True
