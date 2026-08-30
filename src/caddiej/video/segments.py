"""Which second of footage is in which file, and how its clock relates to ours.

The index is the whole of the rolling buffer as far as the rest of CaddieJ is
concerned: append what FFmpeg reports, ask which files cover a window, and drop
what is too old to keep. It is pure and synchronous, so the interesting part —
the arithmetic that decides which frames a swing is in — is testable with no
subprocess, no camera and no waiting.

**On asking for a window, the answer may be a `Gap`.** A caller must not be able
to mistake partial coverage for a clip: footage that was never recorded, footage
already deleted, and a hole left by a restart are different failures and each
one is worth reporting rather than papering over.
"""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

_SEQUENCE = re.compile(r"(\d+)")


@dataclass(frozen=True, slots=True)
class Segment:
    """One file of the rolling buffer.

    `media_start` and `media_end` are FFmpeg's own timeline, seconds since
    capture started. `observed_monotonic` is CaddieJ's reading of when the
    segment was seen to finish, and it is what ties the two clocks together.
    """

    sequence: int
    path: Path
    media_start: float
    media_end: float
    observed_monotonic: float
    size_bytes: int = 0

    @property
    def duration(self) -> float:
        return self.media_end - self.media_start


class Gap(StrEnum):
    """Why a window could not be served."""

    NOT_RECORDING = "not_recording"
    RECYCLED = "recycled"
    NOT_YET_RECORDED = "not_yet_recorded"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SegmentWindow:
    """Whole segments covering a requested window, and what they actually span.

    Requested and covered are both kept because they differ: the covering set
    starts at a segment boundary, so it holds a little more footage than was
    asked for. That surplus is recorded rather than trimmed away, and it is what
    lets a clip's metadata state exactly where impact falls.
    """

    segments: tuple[Segment, ...]
    requested_start: float
    requested_end: float

    @property
    def media_start(self) -> float:
        return self.segments[0].media_start

    @property
    def media_end(self) -> float:
        return self.segments[-1].media_end

    @property
    def duration(self) -> float:
        return self.media_end - self.media_start

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(segment.path for segment in self.segments)


class SegmentIndex:
    """The buffer's contents, newest last, plus the mapping between clocks."""

    def __init__(self, started_monotonic: float, latency_seconds: float = 0.0) -> None:
        self._started_monotonic = started_monotonic
        self._latency = latency_seconds
        self._segments: list[Segment] = []

    def reset(self, started_monotonic: float) -> None:
        self._started_monotonic = started_monotonic
        self._segments.clear()

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)

    @property
    def count(self) -> int:
        return len(self._segments)

    @property
    def total_bytes(self) -> int:
        return sum(segment.size_bytes for segment in self._segments)

    @property
    def span_seconds(self) -> float:
        if not self._segments:
            return 0.0
        return self._segments[-1].media_end - self._segments[0].media_start

    def append(self, segment: Segment) -> None:
        self._segments.append(segment)

    def media_time(self, monotonic: float) -> float:
        """When something that happened at `monotonic` appears in the recording.

        Anchored on the most recently completed segment rather than on the start
        of capture. A single origin would be exact at the start and drift for the
        rest of the evening — a camera running at a nominal frame rate is not
        running at exactly that rate, and after an hour the error is larger than
        the whole clip. Re-anchoring keeps it bounded by roughly how long ago the
        last segment closed.

        Adding the camera's latency, rather than subtracting it, is deliberate:
        an event appears in the footage *later* than it happened.
        """

        if self._segments:
            last = self._segments[-1]
            anchor_media, anchor_monotonic = last.media_end, last.observed_monotonic
        else:
            anchor_media, anchor_monotonic = 0.0, self._started_monotonic
        return anchor_media + (monotonic - anchor_monotonic) + self._latency

    def window(self, start: float, end: float) -> SegmentWindow | Gap:
        """The segments covering a window of media time, or why there are none."""

        if end < start:
            raise ValueError("A window cannot end before it starts")
        if not self._segments:
            return Gap.NOT_RECORDING
        if start < self._segments[0].media_start:
            return Gap.RECYCLED
        if end > self._segments[-1].media_end:
            return Gap.NOT_YET_RECORDED

        covering = [
            segment
            for segment in self._segments
            if segment.media_end > start and segment.media_start < end
        ]
        if not covering:
            # Inside the span but between two segments: capture stopped and
            # restarted around exactly this moment.
            return Gap.INTERRUPTED
        if _has_hole(covering):
            return Gap.INTERRUPTED
        return SegmentWindow(
            segments=tuple(covering), requested_start=start, requested_end=end
        )

    def prune(
        self, keep_seconds: float, max_bytes: int | None = None
    ) -> tuple[Segment, ...]:
        """Drop the oldest segments, returning the ones the caller should delete.

        Bounded twice, by age and by size, because a low-bitrate camera and a
        high-bitrate one need different limits and neither one alone is a
        guarantee. The newest segment is never dropped: an empty buffer would
        report as "not recording" while recording perfectly well.
        """

        evicted: list[Segment] = []
        while len(self._segments) > 1:
            newest_end = self._segments[-1].media_end
            oldest = self._segments[0]
            too_old = newest_end - oldest.media_end > keep_seconds
            too_large = max_bytes is not None and self.total_bytes > max_bytes
            if not (too_old or too_large):
                break
            evicted.append(self._segments.pop(0))
        return tuple(evicted)


class SegmentListReader:
    """Reads FFmpeg's segment index as it is written.

    Byte offsets rather than re-reading the file, and complete lines only: the
    file is being appended to by another process, so the last line may be half
    written at the moment it is read. Consuming it would produce a segment with a
    truncated end time, which is worse than waiting a quarter of a second.
    """

    def __init__(self, path: Path, base_dir: Path) -> None:
        self._path = path
        self._base_dir = base_dir
        self._offset = 0

    def reset(self) -> None:
        self._offset = 0

    def read(self, observed_monotonic: float) -> list[Segment]:
        """Every segment reported since the last call."""

        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("video_segment_list_unreadable")
            return []

        complete, newline, _partial = chunk.rpartition(b"\n")
        if not newline:
            return []
        self._offset += len(complete) + len(newline)

        segments: list[Segment] = []
        for line in complete.decode("utf-8", "replace").splitlines():
            segment = self._parse(line, observed_monotonic)
            if segment is not None:
                segments.append(segment)
        return segments

    def _parse(self, line: str, observed_monotonic: float) -> Segment | None:
        fields = line.strip().split(",")
        if len(fields) < 3:
            return None
        name, start, end = fields[0], fields[1], fields[2]
        try:
            media_start, media_end = float(start), float(end)
        except ValueError:
            logger.warning("video_segment_line_unparseable")
            return None

        path = Path(name)
        if not path.is_absolute():
            path = self._base_dir / path.name
        return Segment(
            sequence=_sequence_of(path),
            path=path,
            media_start=media_start,
            media_end=media_end,
            observed_monotonic=observed_monotonic,
            size_bytes=_size_of(path),
        )


def _sequence_of(path: Path) -> int:
    match = _SEQUENCE.search(path.stem)
    return int(match.group(1)) if match else 0


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _has_hole(segments: list[Segment]) -> bool:
    return any(
        later.sequence != earlier.sequence + 1
        for earlier, later in zip(segments, segments[1:], strict=False)
    )
