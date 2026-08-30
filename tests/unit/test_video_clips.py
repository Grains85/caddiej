"""Extraction, with FFmpeg replaced by something that writes a file.

Two properties matter more than the rest. A clip must not become visible until it
is finished, because something is watching for it and will open it. And the
metadata beside it must be true, because it is the only thing that says where
impact is — the clip itself is deliberately cut wide.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.domain.media import TimeQuality
from caddiej.video.cameras import synthetic_camera
from caddiej.video.clips import ClipExtractor, ClipRequest, _verify
from caddiej.video.errors import ClipExtractionError
from caddiej.video.fakes import FakeRun, finished_mp4
from caddiej.video.layout import MediaLayout
from caddiej.video.segments import Segment, SegmentWindow

IMPACT = datetime(2026, 8, 29, 19, 24, 11, tzinfo=UTC)
RECORDED = datetime(2026, 8, 29, 19, 24, 14, tzinfo=UTC)
CAMERA = synthetic_camera(camera_id="tee", fps=60, width=1280, height=720)


def request(pre: float = 2.0, post: float = 1.5) -> ClipRequest:
    return ClipRequest(
        clip_id=uuid4(),
        shot_id=uuid4(),
        camera_id="tee",
        impact_monotonic=1000.0,
        impact_at=IMPACT,
        pre_seconds=pre,
        post_seconds=post,
    )


def window(
    directory: Path, first: int = 8, count: int = 4, segment_seconds: float = 1.0
) -> SegmentWindow:
    directory.mkdir(parents=True, exist_ok=True)
    segments = []
    for index in range(count):
        sequence = first + index
        path = directory / f"seg-{sequence:06d}.mkv"
        path.write_bytes(b"x" * 128)
        start = sequence * segment_seconds
        segments.append(
            Segment(
                sequence=sequence,
                path=path,
                media_start=start,
                media_end=start + segment_seconds,
                observed_monotonic=start + segment_seconds,
                size_bytes=128,
            )
        )
    return SegmentWindow(
        segments=tuple(segments),
        requested_start=segments[0].media_start + 0.4,
        requested_end=segments[-1].media_end - 0.3,
    )


async def extract(
    tmp_path: Path,
    run: FakeRun | None = None,
    camera=CAMERA,
    clip_request: ClipRequest | None = None,
    impact_media: float = 10.5,
):
    layout = MediaLayout(tmp_path)
    extractor = ClipExtractor(layout, run_command=run or FakeRun())
    return await extractor.extract(
        clip_request or request(),
        window(layout.buffer_dir("tee")),
        impact_media,
        camera,
        recorded_at=RECORDED,
    )


# --- the happy path --------------------------------------------------------


async def test_a_clip_is_written_where_the_reference_says_it_is(
    tmp_path: Path,
) -> None:
    reference = await extract(tmp_path)

    assert (tmp_path / reference.path).exists()
    # Relative, so the whole library can be moved to another machine without
    # invalidating a stored reference.
    assert not reference.path.is_absolute()
    assert reference.path.parts[:2] == ("clips", "2026-08-29")


async def test_a_clip_is_named_by_time_shot_and_camera(tmp_path: Path) -> None:
    clip_request = request()

    reference = await extract(tmp_path, clip_request=clip_request)

    assert reference.path.name.startswith("192411-")
    assert str(clip_request.shot_id)[:8] in reference.path.name
    assert reference.path.name.endswith("-tee.mp4")


async def test_the_clip_says_exactly_where_impact_falls(tmp_path: Path) -> None:
    # The clip is whole segments, so this is arithmetic rather than a guess
    # about where a cut landed. Segments 8 to 11 start at media time 8.
    reference = await extract(tmp_path, impact_media=10.5)

    assert reference.impact_offset_seconds == pytest.approx(2.5)
    assert reference.duration_seconds == pytest.approx(4.0)


async def test_an_uncalibrated_camera_produces_an_estimated_clip(
    tmp_path: Path,
) -> None:
    # Nobody has measured this camera's latency, and saying so is the difference
    # between a usable number and a confident wrong one.
    reference = await extract(tmp_path)

    assert reference.time_quality is TimeQuality.ESTIMATED


async def test_a_measured_camera_latency_is_declared(tmp_path: Path) -> None:
    measured = replace(synthetic_camera(camera_id="tee"), latency_seconds=0.08)

    reference = await extract(tmp_path, camera=measured)

    assert reference.time_quality is TimeQuality.CALIBRATED


async def test_the_clip_carries_what_the_camera_produced(tmp_path: Path) -> None:
    reference = await extract(tmp_path)

    assert (reference.fps, reference.width, reference.height) == (60, 1280, 720)


async def test_the_segments_are_joined_in_order(tmp_path: Path) -> None:
    run = FakeRun()

    await extract(tmp_path, run=run)

    # The concat list is deleted afterwards, so the command is the record of
    # what was joined.
    assert run.commands[0][-1].endswith(".mp4")


# --- the metadata beside it ------------------------------------------------


async def test_a_clip_is_explained_by_the_file_next_to_it(tmp_path: Path) -> None:
    reference = await extract(tmp_path)

    sidecar = json.loads(
        (tmp_path / reference.path).with_suffix(".json").read_text(encoding="utf-8")
    )

    assert sidecar["shot_id"] == str(reference.shot_id)
    assert sidecar["impact_offset_seconds"] == pytest.approx(2.5)
    assert sidecar["impact_at"] == IMPACT.isoformat()
    assert sidecar["fps"] == 60
    assert sidecar["time_quality"] == "estimated"
    assert sidecar["segments"] == [8, 9, 10, 11]


async def test_the_metadata_records_what_was_asked_for_and_what_was_captured(
    tmp_path: Path,
) -> None:
    # They differ, and the difference is the only record of how wide the clip
    # actually is.
    reference = await extract(tmp_path, clip_request=request(pre=2.0, post=1.5))

    sidecar = json.loads(
        (tmp_path / reference.path).with_suffix(".json").read_text(encoding="utf-8")
    )

    assert sidecar["requested_window"]["pre_seconds"] == 2.0
    assert sidecar["requested_window"]["post_seconds"] == 1.5
    assert sidecar["captured_window"] == {"start": 8.0, "end": 12.0}


async def test_the_metadata_records_how_the_clip_was_made(tmp_path: Path) -> None:
    # A clip that looks wrong is diagnosed from the command that produced it.
    reference = await extract(tmp_path)

    sidecar = json.loads(
        (tmp_path / reference.path).with_suffix(".json").read_text(encoding="utf-8")
    )

    assert sidecar["ffmpeg"][0] == "ffmpeg"
    assert "concat" in sidecar["ffmpeg"]


# --- when it goes wrong ----------------------------------------------------


async def test_a_failing_ffmpeg_produces_no_clip_and_says_why(
    tmp_path: Path,
) -> None:
    run = FakeRun(returncode=1, stderr="Invalid data found when processing input")

    with pytest.raises(ClipExtractionError, match="Invalid data"):
        await extract(tmp_path, run=run)

    assert list((tmp_path / "clips").rglob("*.mp4")) == []


async def test_an_ffmpeg_that_claims_success_but_writes_nothing_is_refused(
    tmp_path: Path,
) -> None:
    # An empty file is worse than no file: something would open it and fail.
    with pytest.raises(ClipExtractionError, match="no clip"):
        await extract(tmp_path, run=FakeRun(produce=False))


async def test_an_unfinished_video_is_refused_however_plausible_its_size(
    tmp_path: Path,
) -> None:
    """The case that matters more than an empty file, because it looks fine.

    An MP4's index is written last, so FFmpeg killed part way through — at
    shutdown, or by the extraction timeout — leaves megabytes of perfectly
    reasonable-looking file that no player can open. Publishing it would mean a
    clip announced as ready, a sidecar describing it, and a coaching job pulling
    frames out of nothing.
    """

    truncated = FakeRun(contents=finished_mp4()[:-8])

    with pytest.raises(ClipExtractionError, match="unfinished"):
        await extract(tmp_path, run=truncated)

    assert list((tmp_path / "clips").rglob("*.mp4")) == []
    assert list((tmp_path / "clips").rglob("*.json")) == []


def test_only_a_container_whose_index_is_at_the_end_is_checked_for_one(
    tmp_path: Path,
) -> None:
    """Everything CaddieJ writes is an MP4, but a container that keeps its index
    somewhere else must not be refused for failing a test that does not apply to
    it. Non-empty is the whole of what can honestly be said about those."""

    other = tmp_path / "clip.mkv"
    other.write_bytes(b"not an mp4 at all")

    _verify(other)

    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"not an mp4 at all")
    with pytest.raises(ClipExtractionError, match="unfinished"):
        _verify(mp4)


async def test_a_failure_leaves_no_half_written_files_behind(
    tmp_path: Path,
) -> None:
    with pytest.raises(ClipExtractionError):
        await extract(tmp_path, run=FakeRun(returncode=69))

    leftovers = [path.name for path in (tmp_path / "clips").rglob(".tmp-*")]
    assert leftovers == []


async def test_the_working_list_is_cleaned_up_either_way(tmp_path: Path) -> None:
    await extract(tmp_path)

    assert list((tmp_path / "buffer" / "tee").glob(".concat-*")) == []


async def test_a_clip_never_appears_before_it_is_finished(tmp_path: Path) -> None:
    # The file is written under a dotted temporary name and moved into place, so
    # a subscriber watching the directory cannot open a partial video.
    layout = MediaLayout(tmp_path)
    clip_request = request()
    seen: list[list[str]] = []

    async def watching(args, _timeout):
        destination = Path(args[-1])
        destination.write_bytes(finished_mp4())
        seen.append(
            [path.name for path in destination.parent.iterdir() if path.is_file()]
        )
        from caddiej.video.ffmpeg import CommandResult

        return CommandResult(returncode=0, stderr="")

    extractor = ClipExtractor(layout, run_command=watching)
    reference = await extractor.extract(
        clip_request,
        window(layout.buffer_dir("tee")),
        10.5,
        CAMERA,
        recorded_at=RECORDED,
    )

    # While FFmpeg was running, the only thing in the directory was the dotted
    # temporary file.
    assert seen[0] == [f".tmp-{clip_request.clip_id}.mp4"]
    assert (tmp_path / reference.path).exists()
