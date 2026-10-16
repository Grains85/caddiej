"""Stills of ProTee's swing replay: timing, selection, replacement, failure.

Four subjects, and two of them are the ones that decide whether this feature is
safe to leave switched on during a round.

`TestTheFloorIsNeverWaiting` is the important one. A capture takes several
seconds by design, and the bus worker that delivers `ShotDetected` is the same
one that feeds the floor's readiness — so a real bus and a real `PlayFlow` are
put behind an OBS that hangs, and the shot result has to be on the floor
anyway.

`TestLatestSwingIsReplacedWhole` is the other. There is one swing on disk, so
every way two shots can overlap is a way a consumer can be shown half of one
swing and half of another, or the older of the two. A stale capture that
finishes after a newer one must lose, and it must lose after it has already
written its frames — which is the case that a naive check at the start would
miss.

Nothing here waits a production delay: the clock is fake and the OBS is a stub.
And nothing here can say anything about when ProTee's replay actually appears
on screen. That is what the hardware check and the first real shot are for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

import pytest

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.adapters.obs import (
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
    ObsVersionInfo,
)
from caddiej.core.delivery import Delivery
from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.event_policy import delivery_for, queue_size_for
from caddiej.domain.events import (
    RoundShotObserved,
    ShotDetected,
    SwingFramesReady,
)
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import PlayFlow
from caddiej.floor.geometry import PhysicalPoint
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.swing_capture import (
    CapturedFrame,
    LatestSwingStore,
    SwingCaptureService,
    active_interval,
    difference,
    probe_swing_capture,
    select,
)
from caddiej.swing_capture.service import _SHOTS_BEFORE_TRYING_AGAIN
from caddiej.swing_capture.store import METADATA_NAME

CAMERA_1 = "CaddieJ Swing Camera 1"
CAMERA_2 = "CaddieJ Swing Camera 2"


def a_jpeg(width: int = 3, height: int = 2) -> bytes:
    """The smallest byte string CaddieJ reads as a JPEG of that size.

    A start-of-image marker and a frame header, which is all `jpeg_size` looks
    at, plus an end-of-image marker. Not a decodable picture: nothing in these
    tests renders one, and a real photograph pasted into the source as hex
    would be a blob nobody can review.
    """

    frame = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes(10)
    )
    return b"\xff\xd8" + frame + b"\xff\xd9"


JPEG = a_jpeg()


def a_still_edge(width: int, height: int) -> bytes:
    """A JPEG header of the given size, padded so it is not a blank-frame reject.

    Dimension rejection has to be what stops it. The blank-frame rule only
    looks at byte length.
    """

    payload = a_jpeg(width, height)
    if len(payload) < 800:
        payload += b"\x00" * (800 - len(payload))
    return payload


def a_still(marker: int) -> bytes:
    """A distinct encoded still. Different bytes mean a different picture.

    Padded past the blank-frame threshold. A real black JPEG from OBS is
    smaller than that, and those are rejected; these stand in for a picture.
    """

    payload = JPEG + bytes([marker % 251])
    if len(payload) < 800:
        payload += b"\x00" * (800 - len(payload))
    return payload


def an_impact(trigger_id: str) -> ShotDetected:
    return ShotDetected(trigger_id=trigger_id, source="protee_log")


def a_result(shot_id: str) -> RoundShotObserved:
    return RoundShotObserved(
        shot=ObservedRoundShot(
            shot_id=shot_id,
            ball_speed_raw=32.83863067626953,
            total_distance_raw=28.0288715,
            distance_to_pin_raw=9.007874,
        )
    )


@dataclass
class StubObs:
    """An OBS that answers screenshots, and can refuse, hang, or lose a camera."""

    connect_error: BaseException | None = None
    screenshot_error: BaseException | None = None
    failing_sources: frozenset[str] = frozenset()
    tiny_sources: frozenset[str] = frozenset()
    hang: asyncio.Event | None = None
    static_leading: int = 0
    static_trailing: int = 0
    sizes: tuple[int, ...] = ()
    """Encoded lengths to answer with, one distinct still each, in order."""

    connects: int = 0
    disconnects: int = 0
    requests: list[str] = field(default_factory=list)
    widths: list[int | None] = field(default_factory=list)

    async def connect(self) -> None:
        self.connects += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.connect_error is not None:
            raise self.connect_error

    async def source_screenshot(
        self, source_name: str, *, width: int | None = None
    ) -> bytes:
        self.requests.append(source_name)
        self.widths.append(width)
        if source_name in self.failing_sources:
            raise ObsResourceNotFoundError(f"no source named {source_name}")
        if source_name in self.tiny_sources:
            return a_still_edge(1, 1)
        if self.screenshot_error is not None:
            raise self.screenshot_error
        taken = sum(1 for name in self.requests if name == source_name) - 1
        return self._still(taken)

    def _still(self, taken: int) -> bytes:
        """A held frame, then a moving one, then a held frame again.

        This is what a replay panel looks like from outside: nothing, then the
        swing, then the last frame of the swing sitting there.
        """

        if self.sizes:
            size = self.sizes[min(taken, len(self.sizes) - 1)]
            head = JPEG + taken.to_bytes(4, "big")
            return head + bytes(max(0, size - len(head)))
        if taken < self.static_leading:
            return a_still(0)
        moving = taken - self.static_leading
        if self.static_trailing and moving >= self._moving_count():
            return a_still(240)
        return a_still(moving + 1)

    def _moving_count(self) -> int:
        return max(1, self.frames_total - self.static_leading - self.static_trailing)

    frames_total: int = 0

    async def get_version(self) -> ObsVersionInfo:
        return ObsVersionInfo(
            obs_version="30.2.3",
            websocket_version="5.5.2",
            rpc_version=1,
            supported_image_formats=("jpg", "png"),
        )

    async def disconnect(self) -> None:
        self.disconnects += 1


class Clock:
    """A fake monotonic clock whose sleeps advance it and return at once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, seconds)
        # Still a real await, so anything racing the capture gets its turn.
        await asyncio.sleep(0)


class Table:
    """One configured service, its OBS, its disk, and what it published."""

    def __init__(
        self,
        tmp_path: Path,
        obs: StubObs | None = None,
        *,
        fps: float = 10.0,
        duration: float = 1.0,
        selected: int = 4,
        sources: tuple[str, ...] = (CAMERA_1, CAMERA_2),
        image_width: int | None = None,
    ) -> None:
        self.template = obs or StubObs()
        self.template.frames_total = max(1, round(duration * fps))
        self.clients: list[StubObs] = []
        self.clock = Clock()
        self.published: list[Event] = []
        self.directory = tmp_path / "media" / "latest_swing"
        self.service = SwingCaptureService(
            self._make,
            sources,
            LatestSwingStore(self.directory),
            self._publish,
            enabled=True,
            initial_delay_seconds=0.5,
            duration_seconds=duration,
            fps=fps,
            selected_frames=selected,
            image_width=image_width,
            command_timeout=1.0,
            shutdown_timeout=0.5,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def _make(self) -> StubObs:
        client = StubObs(
            connect_error=self.template.connect_error,
            screenshot_error=self.template.screenshot_error,
            failing_sources=self.template.failing_sources,
            tiny_sources=self.template.tiny_sources,
            hang=self.template.hang,
            static_leading=self.template.static_leading,
            static_trailing=self.template.static_trailing,
            sizes=self.template.sizes,
            frames_total=self.template.frames_total,
        )
        self.clients.append(client)
        return client

    async def _publish(self, event: Event) -> None:
        self.published.append(event)

    async def capture(self, trigger_id: str = "16") -> None:
        """Run one whole capture to completion, on the fake clock."""

        await self.service.on_shot_detected(an_impact(trigger_id))
        await self.finish()

    async def finish(self) -> None:
        task = self.service._task  # noqa: SLF001 — the test owns the lifecycle
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    @property
    def ready(self) -> list[SwingFramesReady]:
        return [
            event for event in self.published if isinstance(event, SwingFramesReady)
        ]

    def metadata(self) -> dict:
        return json.loads((self.directory / METADATA_NAME).read_text(encoding="utf-8"))

    def files(self, camera: str) -> list[str]:
        directory = self.directory / camera
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.glob("*.jpg"))


class TestTheCaptureWindow:
    async def test_a_strike_takes_stills_from_both_sources(self, tmp_path) -> None:
        table = Table(tmp_path, duration=1.0, fps=10.0)

        await table.capture()

        client = table.clients[-1]
        assert client.requests.count(CAMERA_1) == 10
        assert client.requests.count(CAMERA_2) == 10

    async def test_nothing_is_captured_until_the_configured_delay_has_passed(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path)

        await table.service.on_shot_detected(an_impact("16"))
        await asyncio.sleep(0)

        assert table.clock.slept[0] == 0.5
        await table.finish()

    async def test_the_rate_and_the_window_decide_how_many_stills(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=2.0, fps=5.0)

        await table.capture()

        assert table.clients[-1].requests.count(CAMERA_1) == 10

    async def test_the_two_panels_are_requested_at_the_same_moment(
        self, tmp_path
    ) -> None:
        # Interleaved rather than all of camera 1 then all of camera 2: the
        # panels are a synchronised pair and must not drift apart.
        table = Table(tmp_path, duration=0.3, fps=10.0)

        await table.capture()

        assert table.clients[-1].requests == [CAMERA_1, CAMERA_2] * 3

    async def test_a_configured_width_is_passed_to_obs(self, tmp_path) -> None:
        table = Table(tmp_path, duration=0.2, image_width=640)

        await table.capture()

        assert set(table.clients[-1].widths) == {640}

    async def test_the_event_handler_does_not_wait_for_the_capture(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=4.0, fps=10.0)

        await table.service.on_shot_detected(an_impact("16"))

        assert table.ready == [], "the handler waited for the capture to finish"
        await table.finish()

    async def test_a_repeated_trigger_id_does_not_start_a_second_capture(
        self, tmp_path
    ) -> None:
        # A log the observer had to re-read from the start replays its detections.
        table = Table(tmp_path, duration=0.2)

        await table.service.on_shot_detected(an_impact("16"))
        await table.service.on_shot_detected(an_impact("16"))
        await table.finish()

        assert len(table.ready) == 1


class TestChoosingWhichStillsToKeep:
    async def test_a_representative_subset_is_kept_rather_than_everything(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=4.0, fps=10.0, selected=8)

        await table.capture()

        assert table.clients[-1].requests.count(CAMERA_1) == 40
        assert table.files("camera_1") == [
            f"frame_{index:03d}.jpg" for index in range(1, 5)
        ]
        assert table.files("camera_2") == table.files("camera_1")
        assert len(table.files("camera_1")) + len(table.files("camera_2")) == 8

    async def test_the_two_cameras_are_selected_at_the_same_moments(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=4.0, fps=10.0, selected=6)

        await table.capture()

        cameras = table.metadata()["cameras"]
        first = [frame["offset_seconds"] for frame in cameras[0]["frames"]]
        second = [frame["offset_seconds"] for frame in cameras[1]["frames"]]
        assert first == second
        assert first == sorted(first), "frames must stay in temporal order"

    async def test_held_frames_at_each_end_are_trimmed_away(self, tmp_path) -> None:
        """The replay has not started, and later it has ended on a held frame.
        Both are the same picture over and over, and worth nothing to a coach."""

        obs = StubObs(static_leading=10, static_trailing=10)
        table = Table(tmp_path, obs, duration=4.0, fps=10.0, selected=4)

        await table.capture()

        camera = table.metadata()["cameras"][0]
        interval = camera["replay_interval"]
        assert interval["detected"] is True
        assert interval["start_seconds"] == pytest.approx(1.4, abs=0.15)
        assert interval["end_seconds"] == pytest.approx(3.5, abs=0.2)

    async def test_an_animated_loading_screen_is_trimmed_by_its_size(
        self, tmp_path, caplog
    ) -> None:
        """ProTee's spinner never repeats a frame, so only its size gives it
        away. The metadata and the log both say how many stills it was."""

        loading = tuple(80000 + 300 * (index % 4) for index in range(12))
        replay = tuple(150000 + 400 * (index % 5) for index in range(28))
        obs = StubObs(sizes=loading + replay)
        table = Table(tmp_path, obs, duration=4.0, fps=10.0, selected=8)

        with caplog.at_level(logging.INFO, logger="caddiej"):
            await table.capture()

        metadata = table.metadata()
        selection = metadata["selection_interval"]
        assert selection["detected"] is True
        assert selection["pre_replay_frames"] == 12
        for camera in metadata["cameras"]:
            interval = camera["replay_interval"]
            assert interval["detected"] is True
            assert interval["pre_replay_frames"] == 12
            offsets = [frame["offset_seconds"] for frame in camera["frames"]]
            assert offsets
            assert min(offsets) >= interval["start_seconds"]
            assert offsets[0] == pytest.approx(selection["start_seconds"])
        detected = [
            record
            for record in caplog.records
            if record.getMessage() == "swing_replay_interval_detected"
        ]
        assert detected
        assert all(
            getattr(record, "swing_capture_pre_replay_frames", None) == 12
            for record in detected
        )

    async def test_a_panel_that_never_stands_still_falls_back_to_the_whole_window(
        self, tmp_path
    ) -> None:
        """A live view rather than a replay panel. Uniform selection is a
        perfectly good answer and the honest one."""

        table = Table(tmp_path, duration=3.0, fps=10.0, selected=5)

        await table.capture()

        camera = table.metadata()["cameras"][0]
        assert camera["replay_interval"]["detected"] is False
        kept = sum(len(item["frames"]) for item in table.metadata()["cameras"])
        assert kept == 5
        assert len(camera["frames"]) == 3
        assert table.metadata()["selection_interval"]["detected"] is False

    def test_identical_stills_are_the_only_thing_reported_as_unchanged(self) -> None:
        # The metric is a change detector, not a distance: bytes cannot measure
        # how different two pictures are without decoding them.
        assert difference(a_still(1), a_still(1)) == 0.0
        assert difference(a_still(1), a_still(2)) == 1.0

    def test_two_stills_are_never_trimmed_into_an_interval(self) -> None:
        frames = (
            CapturedFrame(0.0, a_still(1)),
            CapturedFrame(0.1, a_still(2)),
        )

        assert active_interval(frames).detected is False

    def test_a_camera_that_lost_stills_still_lines_up_with_the_other(self) -> None:
        """Selection is by timestamp, so a camera missing half its frames
        returns fewer frames at the same moments rather than drifting.

        These two panels have no view, so they have no phase calibration and
        are spread evenly -- the path where lining up is still meaningful. Two
        calibrated views deliberately do NOT line up: the bay's replays are
        not synchronised, and the same offset is a different moment in each."""

        full = [CapturedFrame(index * 0.1, a_still(index)) for index in range(20)]
        sparse = [frame for index, frame in enumerate(full) if index % 2 == 0]

        chosen, _intervals, _combined, _phases = select({"a": full, "b": sparse}, 4)

        first = [full[index].offset_seconds for index in chosen["a"]]
        second = [sparse[index].offset_seconds for index in chosen["b"]]
        # Within one of the sparse camera's own intervals: it cannot do better
        # than the stills it has, and it must not do worse.
        assert first == pytest.approx(second, abs=0.2)

    def test_a_camera_with_nothing_contributes_nothing(self) -> None:
        full = [CapturedFrame(index * 0.1, a_still(index)) for index in range(10)]

        chosen, _intervals, _combined, _phases = select({"a": full, "b": []}, 4)

        assert len(chosen["a"]) == 4
        assert chosen["b"] == ()


class TestLatestSwingIsReplacedWhole:
    async def test_the_capture_is_written_where_it_was_configured(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=0.5, selected=3)

        await table.capture()

        assert (table.directory / METADATA_NAME).is_file()
        assert len(table.files("camera_1")) + len(table.files("camera_2")) == 3

    async def test_a_second_shot_replaces_the_first_swing_entirely(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=0.5, fps=10.0, selected=5)
        await table.capture("16")
        first = table.metadata()["capture_id"]

        table.service._selected = 2  # noqa: SLF001 — a different-sized swing
        await table.capture("17")

        assert table.metadata()["capture_id"] != first
        assert table.metadata()["trigger_id"] == "17"
        # Not five files from the old swing beside the new one's total of two.
        assert len(table.files("camera_1")) + len(table.files("camera_2")) == 2

    async def test_nothing_half_written_is_ever_visible(self, tmp_path) -> None:
        """The staging directory is hidden and a sibling, so a reader that
        globs the media root while a capture is running finds the previous
        swing or nothing — never a directory being filled in."""

        table = Table(tmp_path, duration=0.5, selected=3)
        seen: list[list[str]] = []
        store = table.service._store  # noqa: SLF001 — inspecting the swap

        original = store.stage

        def watching(record):
            staged = original(record)
            seen.append(sorted(path.name for path in table.directory.parent.iterdir()))
            return staged

        store.stage = watching  # type: ignore[method-assign]
        await table.capture()

        assert seen, "the capture was never staged"
        visible = [name for name in seen[0] if not name.startswith(".")]
        assert visible == [], "a half-written capture was visible"
        assert sorted(path.name for path in table.directory.parent.iterdir()) == [
            "latest_swing",
            "swing_shots",
        ], "staging was left behind"

    async def test_a_stale_capture_cannot_overwrite_a_newer_swing(
        self, tmp_path
    ) -> None:
        """The case a check at the start of the capture would miss: the older
        capture has already taken its stills and is about to write them."""

        table = Table(tmp_path, duration=0.5, selected=3)
        store = table.service._store  # noqa: SLF001 — driving the race
        original = store.stage
        staged = threading.Event()
        release = threading.Event()

        def slow(record):
            result = original(record)
            staged.set()
            # Blocking on purpose: `stage` runs on a worker thread, so this is
            # the newer shot arriving mid-write. If it ever ran on the loop
            # instead, this test would hang rather than quietly pass.
            assert release.wait(timeout=5)
            return result

        store.stage = slow  # type: ignore[method-assign]
        await table.service.on_shot_detected(an_impact("16"))
        first = table.service._task  # noqa: SLF001
        assert await asyncio.to_thread(staged.wait, 5)

        store.stage = original  # type: ignore[method-assign]
        await table.service.on_shot_detected(an_impact("17"))
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await table.finish()

        assert table.metadata()["trigger_id"] == "17"
        assert [event.capture.trigger_id for event in table.ready] == ["17"]
        assert table.service.captures_cancelled >= 1

    async def test_a_newer_strike_cancels_a_capture_in_flight(self, tmp_path) -> None:
        table = Table(tmp_path, duration=4.0, fps=10.0, selected=4)

        await table.service.on_shot_detected(an_impact("16"))
        first = table.service._task  # noqa: SLF001
        await asyncio.sleep(0)
        await table.service.on_shot_detected(an_impact("17"))
        await asyncio.gather(first, return_exceptions=True)
        await table.finish()

        assert [event.capture.trigger_id for event in table.ready] == ["17"]

    async def test_a_staging_directory_from_a_crashed_run_is_swept_at_start(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=0.2)
        leftover = table.directory.parent / ".latest_swing.staging-deadbeef"
        leftover.mkdir(parents=True)
        (leftover / "frame_001.jpg").write_bytes(JPEG)

        table.service.start()

        assert not leftover.exists()


class TestWhatTheMetadataSays:
    async def test_it_records_the_shot_the_timing_and_the_frames(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=1.0, fps=10.0, selected=4)
        detection = an_impact("16")

        await table.service.on_shot_detected(detection)
        await table.finish()

        payload = table.metadata()
        assert payload["trigger_id"] == "16"
        assert payload["detected_at"] == detection.occurred_at.isoformat()
        assert payload["ok"] is True
        assert payload["cameras_ready"] == 2
        assert payload["configuration"]["fps"] == 10.0
        assert payload["configuration"]["initial_delay_seconds"] == 0.5
        assert payload["configuration"]["sources"] == {
            "camera_1": CAMERA_1,
            "camera_2": CAMERA_2,
        }
        frames = payload["cameras"][0]["frames"]
        assert [frame["index"] for frame in frames] == [1, 2]
        assert [frame["file"] for frame in frames] == [
            f"camera_1/frame_{index:03d}.jpg" for index in range(1, 3)
        ]
        assert sum(len(item["frames"]) for item in payload["cameras"]) == 4
        assert payload["cameras"][0]["captured_frames"] == 10

    async def test_a_frame_carries_the_size_an_annotation_would_be_drawn_on(
        self, tmp_path
    ) -> None:
        # Normalized annotations are only meaningful next to a pixel size.
        table = Table(tmp_path, duration=0.3, selected=2)

        await table.capture()

        frame = table.metadata()["cameras"][0]["frames"][0]
        assert (frame["width"], frame["height"]) == (3, 2)

    async def test_it_does_not_copy_shot_measurements_it_does_not_own(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, duration=0.3, selected=2)

        await table.capture()

        text = (table.directory / METADATA_NAME).read_text(encoding="utf-8")
        for name in ("ball_speed", "carry", "club", "total_distance"):
            assert name not in text


class TestWhenSomethingIsBroken:
    async def test_one_camera_missing_does_not_discard_the_other(
        self, tmp_path
    ) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.5, selected=3)

        await table.capture()

        assert len(table.files("camera_1")) == 3
        assert table.files("camera_2") == []
        payload = table.metadata()
        assert payload["ok"] is False
        assert payload["cameras_ready"] == 1
        assert payload["cameras"][1]["failed_frames"] == 5
        assert table.ready, "half a swing is still worth announcing"

    async def test_a_missing_camera_is_named_once_rather_than_once_per_frame(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_2}))
        table = Table(tmp_path, obs, duration=1.0, fps=10.0, selected=3)

        with caplog.at_level(logging.WARNING):
            await table.capture()

        named = [
            record
            for record in caplog.records
            if record.message == "swing_capture_camera_unavailable"
        ]
        assert len(named) == 1
        assert getattr(named[0], "swing_capture_source") == CAMERA_2

    async def test_a_one_pixel_frame_is_rejected_and_the_other_camera_is_kept(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        obs = StubObs(tiny_sources=frozenset({CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.2, fps=10.0, selected=2)

        with caplog.at_level(logging.WARNING):
            await table.capture()

        assert table.files("camera_1")
        assert table.files("camera_2") == []
        assert any(
            record.message == "invalid_capture_dimensions" for record in caplog.records
        )
        assert table.ready

    async def test_an_obs_that_cannot_be_reached_writes_nothing_and_says_so(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        obs = StubObs(connect_error=ObsTransportConnectionError("obs down"))
        table = Table(tmp_path, obs, duration=0.5, selected=3)

        with caplog.at_level(logging.ERROR):
            await table.capture()

        assert not table.directory.exists()
        assert table.ready == []
        assert table.service.captures_failed == 1
        assert any(
            record.message == "swing_capture_failed" for record in caplog.records
        )

    async def test_an_obs_that_stops_answering_leaves_the_previous_swing_alone(
        self, tmp_path
    ) -> None:
        """Nothing new was captured, so nothing is replaced. The metadata's own
        trigger id says which swing the directory holds."""

        table = Table(tmp_path, duration=0.5, selected=3)
        await table.capture("16")

        failure = ObsTransportConnectionError("obs down")
        table.template.connect_error = failure
        table.clients[-1].screenshot_error = failure
        await table.capture("17")

        assert table.metadata()["trigger_id"] == "16"

    async def test_shutdown_cancels_a_capture_in_flight(self, tmp_path) -> None:
        table = Table(tmp_path, duration=4.0, fps=10.0)

        await table.service.on_shot_detected(an_impact("16"))
        await _until(lambda: bool(table.clients))
        await table.service.aclose()

        assert table.ready == []
        assert not table.directory.exists()
        assert table.clients[-1].disconnects == 1

    async def test_a_disabled_service_never_talks_to_obs(self, tmp_path) -> None:
        def boom() -> StubObs:
            raise AssertionError("OBS must not be contacted when disabled")

        service = SwingCaptureService(
            boom,
            (CAMERA_1, CAMERA_2),
            LatestSwingStore(tmp_path / "latest_swing"),
            _ignore,
            enabled=False,
        )
        bus = AsyncEventBus()
        service.subscribe(bus)
        service.start()

        await service.on_shot_detected(an_impact("16"))
        await service.aclose()

        assert not (tmp_path / "latest_swing").exists()

    async def test_no_configured_source_means_the_feature_is_off(
        self, tmp_path
    ) -> None:
        table = Table(tmp_path, sources=("", ""))

        assert table.service.enabled is False

    async def test_enabled_with_no_panel_source_says_why(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom() -> StubObs:
            raise AssertionError("OBS must not be contacted when there is no panel")

        service = SwingCaptureService(
            boom,
            (),
            LatestSwingStore(tmp_path / "latest_swing"),
            _ignore,
            enabled=True,
        )
        service.subscribe(AsyncEventBus())
        with caplog.at_level(logging.INFO):
            service.start()
            await service.on_shot_detected(an_impact("9"))
            await service.aclose()

        assert service.enabled is False
        details = [
            str(getattr(record, "swing_capture_detail", ""))
            for record in caplog.records
            if record.getMessage() == "swing_capture_skipped"
        ]
        assert details
        assert any("no panel source" in detail for detail in details)


class TestAMisconfiguredCameraNameIsNotASocketFailure:
    """The bug that opened 19,730 OBS connections in one session.

    Two source names in the `.env` did not match anything in the scene. Every
    screenshot raised `ObsResourceNotFoundError`, and the handler for that
    threw the WebSocket away and reconnected — for every frame of every
    capture, all session. The socket was never the problem: a name that does
    not match will not match on the next frame either, so the only thing a
    reconnect buys is another handshake.

    Three separate defences, because the storm needed all three to be absent:
    the name failure is not a transport failure, the two cameras share one
    connection attempt, and a subsystem that has produced nothing three times
    running stops trying.
    """

    async def test_a_missing_source_does_not_throw_the_connection_away(
        self, tmp_path
    ) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_1, CAMERA_2}))
        table = Table(tmp_path, obs, duration=1.0, fps=10.0)

        await table.capture()

        requested = len(table.clients[-1].requests)
        assert requested >= 10, "the frames were still attempted"
        assert len(table.clients) == 1, (
            f"{requested} failed screenshots opened {len(table.clients)} sockets"
        )

    async def test_a_transport_failure_still_throws_the_connection_away(
        self, tmp_path
    ) -> None:
        """The distinction the fix rests on. A half-open socket is worth
        replacing; a name that does not match is not."""

        obs = StubObs(screenshot_error=ObsTransportConnectionError("socket died"))
        table = Table(tmp_path, obs, duration=0.5, fps=10.0)

        await table.capture()

        assert len(table.clients) > 1

    async def test_the_two_cameras_share_one_connection_attempt(self, tmp_path) -> None:
        # Both cameras are shot from the same frame loop. Without a lock around
        # the lazy connect they each opened a client and one was orphaned.
        table = Table(tmp_path, duration=1.0, fps=10.0)

        await table.capture()

        assert len(table.clients) == 1
        assert table.clients[0].connects == 1

    async def test_three_barren_captures_suspend_the_subsystem(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_1, CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.5, fps=10.0)

        with caplog.at_level(logging.ERROR):
            for trigger in ("16", "17", "18"):
                await table.capture(trigger)
        opened = len(table.clients)
        await table.capture("19")

        assert table.service.health().suspended is True
        assert len(table.clients) == opened, "a suspended capture contacts nothing"
        assert [record.message for record in caplog.records].count(
            "swing_capture_suspended"
        ) == 1

    async def test_a_capture_that_works_clears_the_count(self, tmp_path) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_1, CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.5, fps=10.0, selected=3)

        await table.capture("16")
        await table.capture("17")
        table.template.failing_sources = frozenset()
        table.clients[-1].failing_sources = frozenset()
        await table.capture("18")
        table.template.failing_sources = frozenset({CAMERA_1, CAMERA_2})
        table.clients[-1].failing_sources = frozenset({CAMERA_1, CAMERA_2})
        await table.capture("19")

        assert table.service.health().suspended is False
        assert table.service.health().barren_captures == 1

    async def test_standing_down_is_not_giving_up_for_the_session(
        self, tmp_path
    ) -> None:
        """OBS still starting while CaddieJ already runs is the ordinary
        Windows sequence. Three barren captures inside that window used to
        cost the whole evening every picture of a swing, and since the launch
        monitor's replay is the only image source a bay without its own camera
        has, that is everything a coach had to look at.
        """

        obs = StubObs(failing_sources=frozenset({CAMERA_1, CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.5, fps=10.0, selected=3)

        for trigger in ("16", "17", "18"):
            await table.capture(trigger)
        assert table.service.health().suspended is True

        # OBS finishes starting. Nothing tells CaddieJ; it has to look.
        table.template.failing_sources = frozenset()
        for client in table.clients:
            client.failing_sources = frozenset()

        for trigger in range(20, 20 + _SHOTS_BEFORE_TRYING_AGAIN):
            await table.capture(str(trigger))

        assert table.service.health().suspended is False
        assert table.service.health().captures_completed >= 1

    async def test_it_does_not_reach_for_obs_on_every_strike(self, tmp_path) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_1, CAMERA_2}))
        table = Table(tmp_path, obs, duration=0.5, fps=10.0)

        for trigger in ("16", "17", "18"):
            await table.capture(trigger)
        opened = len(table.clients)

        # One short of the retry threshold, so none of these may try.
        for trigger in range(20, 20 + _SHOTS_BEFORE_TRYING_AGAIN - 1):
            await table.capture(str(trigger))

        assert len(table.clients) == opened


class TestTheFloorIsNeverWaiting:
    """The hard requirement: taking pictures cannot delay a shot result.

    Real bus, real `PlayFlow`, real presenter, and a capture configured to run
    for longer than any test here is allowed to take.
    """

    def stack(
        self, tmp_path: Path, obs: StubObs
    ) -> tuple[AsyncEventBus, FloorService, SwingCaptureService]:
        bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
        floor = FloorService()
        presenter = FloorPresenter(
            floor,
            layout_from_tees(
                tee=PhysicalPoint(x_m=0.5, y_m=0.72),
                driver_tee=PhysicalPoint(x_m=0.5, y_m=0.72),
            ),
            club_path_length=0.4,
        )
        play = PlayFlow(
            floor=presenter,
            publish=bus.publish,
            shot_result_min_duration=8.0,
            shot_result_max_duration=30.0,
            sleep=_never,
        )
        play.subscribe(bus)

        service = SwingCaptureService(
            lambda: obs,
            (CAMERA_1, CAMERA_2),
            LatestSwingStore(tmp_path / "latest_swing"),
            bus.publish,
            enabled=True,
            # Far longer than this test is allowed to take.
            initial_delay_seconds=10.0,
            duration_seconds=10.0,
            fps=10.0,
            command_timeout=10.0,
            shutdown_timeout=0.2,
        )
        service.subscribe(bus)
        return bus, floor, service

    async def result_after_a_shot(
        self, tmp_path: Path, obs: StubObs
    ) -> tuple[dict, float]:
        bus, floor, service = self.stack(tmp_path, obs)
        await bus.start()
        service.start()
        started = monotonic()
        try:
            await bus.publish(an_impact("16"))
            await bus.publish(a_result("A"))
            assert await bus.drain(timeout=2.0)
            elapsed = monotonic() - started
            payload = snapshot_message(floor.state).model_dump(mode="json")
        finally:
            await service.aclose()
            await bus.stop(timeout=1.0)
        return payload["state"], elapsed

    async def test_the_result_is_on_the_floor_while_a_capture_is_running(
        self, tmp_path
    ) -> None:
        state, elapsed = await self.result_after_a_shot(tmp_path, StubObs())

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert elapsed < 1.0, f"the result waited {elapsed:.2f}s on a capture"

    async def test_an_obs_that_never_answers_cannot_hold_up_the_result(
        self, tmp_path
    ) -> None:
        # A connection OBS accepts and then does nothing with is the worst case.
        hanging = StubObs(hang=asyncio.Event())

        state, elapsed = await self.result_after_a_shot(tmp_path, hanging)

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert elapsed < 1.0

    async def test_a_detection_the_bus_cannot_deliver_is_dropped_not_queued(
        self,
    ) -> None:
        """The publisher is the ProTee observer, which also feeds the address
        HUD. It may never be made to wait on a subsystem that takes seconds."""

        assert delivery_for("swing_capture", ShotDetected) is Delivery.DROP


class TestTheRealObsDiagnostic:
    """`caddiej --check-swing-capture`, which needs no golf shot.

    It separates "the OBS sources are wrong" from "the timing is wrong", and
    those look identical from the bay.
    """

    async def probe(self, obs: StubObs, tmp_path: Path, **kwargs):
        return await probe_swing_capture(
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            sources=kwargs.pop("sources", (CAMERA_1, CAMERA_2)),
            output_root=tmp_path / "out",
            client_factory=lambda: obs,
            **kwargs,
        )

    async def test_it_saves_a_still_from_each_source_and_reports_the_size(
        self, tmp_path
    ) -> None:
        probe = await self.probe(StubObs(), tmp_path)

        written = sorted(
            path.name
            for path in (tmp_path / "out" / "diagnostics" / "swing-capture").rglob(
                "*.jpg"
            )
        )
        assert probe.ok
        assert written == ["camera_1.jpg", "camera_2.jpg"]
        assert "3 × 2" in probe.report
        assert "OBS 30.2.3" in probe.report

    async def test_a_one_pixel_image_is_a_failed_capture(self, tmp_path) -> None:
        obs = StubObs()
        obs.tiny_sources = frozenset({CAMERA_1, CAMERA_2})

        probe = await self.probe(obs, tmp_path)

        assert not probe.ok
        assert "invalid_capture_dimensions" in probe.report
        assert "capture: OK" not in probe.report
        assert "1×1" in probe.report

    async def test_a_missing_source_is_named(self, tmp_path) -> None:
        obs = StubObs(failing_sources=frozenset({CAMERA_2}))

        probe = await self.probe(obs, tmp_path)

        assert not probe.ok
        assert CAMERA_2 in probe.report
        assert "FAILED at GetSourceScreenshot" in probe.report

    async def test_an_unreachable_obs_points_at_the_websocket_settings(
        self, tmp_path
    ) -> None:
        obs = StubObs(connect_error=ObsTransportConnectionError("refused"))

        probe = await self.probe(obs, tmp_path)

        assert not probe.ok
        assert "FAILED at connect" in probe.report
        assert "WebSocket Server Settings" in probe.report

    async def test_no_configured_source_says_which_variables_to_set(
        self, tmp_path
    ) -> None:
        probe = await self.probe(StubObs(), tmp_path, sources=("", ""))

        assert not probe.ok
        assert "SWING_CAPTURE_PANEL_" in probe.report

    async def test_the_password_is_never_in_the_report(self, tmp_path) -> None:
        for obs in (
            StubObs(),
            StubObs(connect_error=ObsTransportConnectionError("refused")),
            StubObs(failing_sources=frozenset({CAMERA_1})),
        ):
            probe = await self.probe(obs, tmp_path)

            assert "not-printed" not in probe.report


async def _until(condition) -> None:
    """Let the capture task run until it has reached the point being tested."""

    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the capture never got that far")


async def _ignore(_event: object) -> None:
    return None


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()
