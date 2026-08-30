"""The whole video pipeline, with a real FFmpeg and no camera.

Nothing is faked below the subsystem's own front door here: FFmpeg really runs,
segments really land on disk, and a clip is really cut out of them. Only the
photons are missing, which is what makes this runnable on a machine with nothing
plugged in — including, deliberately, whatever machine somebody develops this on.

Gated like the other integration tests because it spawns processes and writes
tens of megabytes. Everything it proves about arithmetic and failure handling is
also covered by the unit tests; what it adds is the answer to "do the commands
we build actually work".
"""

import asyncio
import json
import os
import shutil
import time
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
from caddiej.video import (
    CameraSpec,
    CaptureProfile,
    build_video_service,
    synthetic_camera,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to run a real FFmpeg",
    ),
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None,
        reason="FFmpeg is not installed",
    ),
]

# Small and short: enough footage to cut a clip out of, without spending an
# evening of disk on a test.
CAMERA = synthetic_camera(camera_id="tee", fps=15, width=320, height=180)
PROFILE = CaptureProfile(
    segment_seconds=0.5,
    keyframe_seconds=0.25,
    buffer_seconds=10.0,
    bitrate="1M",
)


class Published:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of[EventType: Event](self, kind: type[EventType]) -> list[EventType]:
        return [event for event in self.events if isinstance(event, kind)]


async def until(condition, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() >= deadline:
            raise AssertionError("it never happened")
        await asyncio.sleep(0.05)


class Filming:
    """A real video subsystem, on a real disk, with a generated camera."""

    def __init__(self, root: Path, camera: CameraSpec = CAMERA) -> None:
        self.root = root
        self.published = Published()
        self.service = build_video_service(
            enabled=True,
            media_root=root,
            camera=camera,
            publish=self.published,
            profile=PROFILE,
            pre_seconds=1.0,
            post_seconds=0.5,
            restart_limit=1,
        )

    async def __aenter__(self) -> "Filming":
        await self.service.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.service.aclose()

    def swing(self, make_shot: ShotBuilder, ago: float = 1.0) -> ShotCompleted:
        """A shot that was hit `ago` seconds ago, on the clock video uses."""

        return ShotCompleted(shot=make_shot(observed_monotonic=time.monotonic() - ago))


async def test_a_swing_becomes_a_playable_clip(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    async with Filming(tmp_path) as filming:
        # Enough footage that a swing a second ago is comfortably inside it.
        await until(lambda: filming.service.health().segments_held >= 6)

        await filming.service.on_shot_completed(filming.swing(make_shot, ago=1.0))
        await until(lambda: filming.published.of(SwingClipReady))

        clip = filming.published.of(SwingClipReady)[0].clip
        path = tmp_path / clip.path
        assert path.exists()
        # A real encoded clip of a second and a half, not an empty container.
        assert path.stat().st_size > 5_000
        assert clip.duration_seconds >= 1.5
        assert 1.0 <= clip.impact_offset_seconds <= 1.0 + PROFILE.segment_seconds


async def test_the_clip_is_explained_by_the_file_beside_it(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    async with Filming(tmp_path) as filming:
        await until(lambda: filming.service.health().segments_held >= 6)
        event = filming.swing(make_shot, ago=1.0)

        await filming.service.on_shot_completed(event)
        await until(lambda: filming.published.of(SwingClipReady))

        clip = tmp_path / filming.published.of(SwingClipReady)[0].clip.path
        sidecar = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))

        assert sidecar["shot_id"] == str(event.shot.shot_id)
        assert sidecar["fps"] == 15
        assert sidecar["requested_window"]["pre_seconds"] == 1.0
        # The captured window is wider than the requested one, because the clip
        # is a whole number of segments.
        captured = sidecar["captured_window"]
        assert captured["end"] - captured["start"] >= 1.5
        assert len(sidecar["segments"]) >= 3


async def test_the_buffer_stops_growing(tmp_path: Path) -> None:
    # Left unbounded this fills the disk overnight. The limit is in seconds of
    # footage, so it holds regardless of what the camera's bitrate turns out
    # to be.
    async with Filming(tmp_path) as filming:
        # Ten seconds of buffer at half-second segments is twenty files; wait
        # until it has had to start deleting.
        await until(lambda: filming.service.health().segments_recycled >= 2, timeout=40)

        health = filming.service.health()
        assert health.buffer_seconds <= PROFILE.buffer_seconds + 1.0
        held = list((tmp_path / "buffer" / "tee").glob("seg-*.mkv"))
        assert len(held) <= health.segments_held + 2


async def test_a_shot_on_the_bus_is_all_it_takes(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    bus = AsyncEventBus()
    await bus.start()
    try:
        async with Filming(tmp_path) as filming:
            filming.service.subscribe(bus)
            await until(lambda: filming.service.health().segments_held >= 6)

            await bus.publish(filming.swing(make_shot, ago=1.0))
            await until(lambda: filming.published.of(SwingClipReady))
    finally:
        await bus.stop()


async def test_a_camera_that_does_not_exist_stops_nothing(
    tmp_path: Path, make_shot: ShotBuilder
) -> None:
    # The failure a real installation is most likely to hit: a device index that
    # is wrong, or a camera another program already has open.
    missing = CameraSpec(
        camera_id="tee",
        input_arguments=("-f", "lavfi", "-i", "no-such-filter=nonsense"),
        fps=15,
        width=320,
        height=180,
    )

    async with Filming(tmp_path, camera=missing) as filming:
        await until(
            lambda: filming.service.health().state == CaptureState.UNAVAILABLE.value,
            timeout=30,
        )

        # It gave up, said so, and is still there to be asked.
        states = [event.state for event in filming.published.of(CameraStateChanged)]
        assert CaptureState.UNAVAILABLE in states
        assert filming.service.health().camera_restarts >= 1

        # And a shot still goes through, reported rather than silently lost.
        await filming.service.on_shot_completed(filming.swing(make_shot))
        await until(lambda: filming.published.of(SwingClipFailed))
        assert (
            filming.published.of(SwingClipFailed)[0].reason is ClipFailure.NOT_RECORDING
        )


async def test_shutting_down_leaves_no_ffmpeg_behind(tmp_path: Path) -> None:
    filming = Filming(tmp_path)
    await filming.service.open()
    await until(lambda: filming.service.health().segments_held >= 2)

    await asyncio.wait_for(filming.service.aclose(), timeout=15.0)

    assert filming.service.health().state == CaptureState.DISABLED.value
    # Nothing is still writing: the newest segment stops growing.
    newest = max(
        (tmp_path / "buffer" / "tee").glob("seg-*.mkv"), key=lambda p: p.stat().st_mtime
    )
    size = newest.stat().st_size
    await asyncio.sleep(1.0)
    assert newest.stat().st_size == size
