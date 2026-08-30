"""The rolling buffer's arithmetic, with no camera and no waiting.

This is where a clip is decided: which files a swing is in, and how a moment on
CaddieJ's clock becomes a moment in a recording. Both are cheap to get wrong in a
way that produces a plausible clip of the wrong two seconds, so they are tested
here rather than inferred from watching video.
"""

from pathlib import Path

import pytest

from caddiej.video.segments import (
    Gap,
    Segment,
    SegmentIndex,
    SegmentListReader,
    SegmentWindow,
)


def segment(
    sequence: int,
    start: float,
    duration: float = 1.0,
    observed: float | None = None,
    size_bytes: int = 1000,
) -> Segment:
    return Segment(
        sequence=sequence,
        path=Path(f"/media/buffer/tee/seg-{sequence:06d}.mkv"),
        media_start=start,
        media_end=start + duration,
        # A segment is seen to have finished when its end arrives, so unless a
        # test says otherwise the two clocks agree.
        observed_monotonic=start + duration if observed is None else observed,
        size_bytes=size_bytes,
    )


def index_of(count: int, started: float = 0.0) -> SegmentIndex:
    index = SegmentIndex(started_monotonic=started)
    for sequence in range(count):
        index.append(segment(sequence, start=started + sequence))
    return index


# --- the two clocks --------------------------------------------------------


def test_before_anything_is_recorded_time_is_measured_from_the_start() -> None:
    index = SegmentIndex(started_monotonic=1000.0)

    assert index.media_time(1002.5) == pytest.approx(2.5)


def test_time_is_anchored_on_the_most_recent_segment() -> None:
    # A single origin would be exact at the start and drift for the rest of the
    # evening: a camera at a nominal frame rate is not at exactly that rate, and
    # after an hour the error is bigger than the whole clip.
    index = SegmentIndex(started_monotonic=1000.0)
    # An hour of capture where the camera's clock has fallen 3 seconds behind
    # ours: media time 3597 was reached when our clock said 3600.
    index.append(segment(3600, start=3596.0, observed=4600.0))

    assert index.media_time(4600.0) == pytest.approx(3597.0)
    assert index.media_time(4601.0) == pytest.approx(3598.0)


def test_camera_latency_puts_an_event_later_in_the_recording() -> None:
    # Exposure, transfer and encoding all happen after the light arrives, so a
    # swing appears in the footage later than it happened.
    index = SegmentIndex(started_monotonic=0.0, latency_seconds=0.1)

    assert index.media_time(5.0) == pytest.approx(5.1)


# --- finding the footage ---------------------------------------------------


def test_a_window_returns_the_segments_covering_it() -> None:
    index = index_of(10)

    window = index.window(3.4, 5.2)

    assert isinstance(window, SegmentWindow)
    assert [seg.sequence for seg in window.segments] == [3, 4, 5]


def test_a_window_reports_what_it_asked_for_and_what_it_got() -> None:
    # They differ: the clip starts on a segment boundary, so it holds a little
    # more than was requested, and a clip's metadata depends on knowing both.
    index = index_of(10)

    window = index.window(3.4, 5.2)

    assert isinstance(window, SegmentWindow)
    assert (window.requested_start, window.requested_end) == (3.4, 5.2)
    assert (window.media_start, window.media_end) == (3.0, 6.0)
    assert window.duration == pytest.approx(3.0)


def test_a_window_inside_one_segment_is_that_segment() -> None:
    index = index_of(5)

    window = index.window(2.1, 2.4)

    assert isinstance(window, SegmentWindow)
    assert [seg.sequence for seg in window.segments] == [2]


def test_a_window_landing_exactly_on_a_boundary_takes_the_later_segment() -> None:
    index = index_of(5)

    window = index.window(2.0, 3.0)

    assert isinstance(window, SegmentWindow)
    assert [seg.sequence for seg in window.segments] == [2]


def test_nothing_recorded_says_so() -> None:
    assert SegmentIndex(0.0).window(1.0, 2.0) is Gap.NOT_RECORDING


def test_footage_already_deleted_says_so() -> None:
    # The swing happened, was recorded, and the buffer recycled it before
    # anyone asked. That is a different failure from never filming it.
    index = SegmentIndex(0.0)
    for sequence in range(5, 10):
        index.append(segment(sequence, start=float(sequence)))

    assert index.window(2.0, 3.0) is Gap.RECYCLED


def test_footage_not_yet_written_says_so() -> None:
    index = index_of(5)

    assert index.window(4.5, 8.0) is Gap.NOT_YET_RECORDED


def test_a_hole_left_by_a_restart_says_so() -> None:
    # Capture stopped and started again inside the window. The segments either
    # side are real, but joining them would silently skip the missing time.
    index = SegmentIndex(0.0)
    index.append(segment(0, start=0.0))
    index.append(segment(1, start=1.0))
    index.append(segment(7, start=2.0))

    assert index.window(0.5, 2.5) is Gap.INTERRUPTED


def test_a_window_that_ends_before_it_starts_is_a_mistake() -> None:
    with pytest.raises(ValueError, match="cannot end before"):
        index_of(3).window(2.0, 1.0)


# --- keeping the buffer bounded --------------------------------------------


def test_segments_past_the_window_are_evicted_oldest_first() -> None:
    index = index_of(10)

    evicted = index.prune(keep_seconds=3.0)

    assert [seg.sequence for seg in evicted] == [0, 1, 2, 3, 4, 5]
    assert [seg.sequence for seg in index.segments] == [6, 7, 8, 9]


def test_a_buffer_is_also_bounded_by_size() -> None:
    # A low-bitrate camera and a high-bitrate one need different limits, and
    # neither age nor size alone is a guarantee.
    index = index_of(10)

    evicted = index.prune(keep_seconds=3600.0, max_bytes=4000)

    assert len(evicted) == 6
    assert index.total_bytes <= 4000


def test_the_newest_segment_is_never_evicted() -> None:
    # An empty buffer reports as "not recording" while recording perfectly well.
    index = index_of(3)

    index.prune(keep_seconds=0.0, max_bytes=0)

    assert index.count == 1


def test_nothing_is_evicted_from_a_buffer_within_its_limits() -> None:
    index = index_of(4)

    assert index.prune(keep_seconds=30.0) == ()
    assert index.count == 4


def test_a_reset_forgets_everything() -> None:
    # Capture restarting means FFmpeg's clock restarts at zero, so the old
    # segments cannot be placed in time any more.
    index = index_of(5)

    index.reset(started_monotonic=99.0)

    assert index.count == 0
    assert index.media_time(100.0) == pytest.approx(1.0)


def test_the_buffer_reports_how_much_it_holds() -> None:
    index = index_of(4)

    assert index.span_seconds == pytest.approx(4.0)
    assert index.total_bytes == 4000


# --- reading FFmpeg's index ------------------------------------------------


def listing(directory: Path, *lines: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "segments.csv"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_a_reader_returns_the_segments_ffmpeg_has_reported(tmp_path: Path) -> None:
    (tmp_path / "seg-000000.mkv").write_bytes(b"x" * 64)
    path = listing(tmp_path, "seg-000000.mkv,0.000000,1.000000\n")

    segments = SegmentListReader(path, tmp_path).read(observed_monotonic=10.0)

    assert len(segments) == 1
    assert segments[0].sequence == 0
    assert segments[0].media_end == pytest.approx(1.0)
    assert segments[0].observed_monotonic == 10.0
    assert segments[0].size_bytes == 64
    assert segments[0].path == tmp_path / "seg-000000.mkv"


def test_a_reader_returns_each_segment_once(tmp_path: Path) -> None:
    path = listing(tmp_path, "seg-000000.mkv,0.0,1.0\n")
    reader = SegmentListReader(path, tmp_path)

    assert len(reader.read(1.0)) == 1
    assert reader.read(2.0) == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("seg-000001.mkv,1.0,2.0\n")

    assert [seg.sequence for seg in reader.read(3.0)] == [1]


def test_a_half_written_line_is_left_until_it_is_finished(tmp_path: Path) -> None:
    # The file is being appended to by another process, so the last line may be
    # half written. Consuming it would produce a segment with a truncated end
    # time, which is worse than waiting a quarter of a second.
    path = listing(tmp_path, "seg-000000.mkv,0.0,1.0\n", "seg-000001.mkv,1.0,2")
    reader = SegmentListReader(path, tmp_path)

    assert [seg.sequence for seg in reader.read(1.0)] == [0]

    path.write_text(
        "seg-000000.mkv,0.0,1.0\nseg-000001.mkv,1.0,2.0\n", encoding="utf-8"
    )

    assert [seg.sequence for seg in reader.read(2.0)] == [1]


def test_a_missing_index_is_not_an_error(tmp_path: Path) -> None:
    # It does not exist until FFmpeg closes its first segment.
    reader = SegmentListReader(tmp_path / "segments.csv", tmp_path)

    assert reader.read(1.0) == []


def test_an_unparseable_line_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    path = listing(
        tmp_path,
        "nonsense\n",
        "seg-000001.mkv,not-a-number,2.0\n",
        "seg-000002.mkv,2.0,3.0\n",
    )

    segments = SegmentListReader(path, tmp_path).read(1.0)

    assert [seg.sequence for seg in segments] == [2]
