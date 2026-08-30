"""Turning part of the buffer into a clip, and saying what the clip is.

Two rules here.

**A clip is never visible before it is finished.** FFmpeg writes to a dotted
temporary name and the file is moved into place with a single rename, so nothing
watching the directory can open a half-written video.

**A clip explains itself.** The sidecar beside it holds where impact falls, what
window was asked for, what was actually captured, and the command that produced
it. It exists so that a clip stays interpretable with the database gone, on
another machine, by something that was not running when it was recorded — which
is also why every time in it is wall clock rather than a monotonic reading.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from caddiej.domain.media import ClipRef, TimeQuality
from caddiej.video.cameras import CameraSpec
from caddiej.video.errors import ClipExtractionError
from caddiej.video.ffmpeg import (
    Run,
    concat_command,
    concat_file_contents,
    run,
)
from caddiej.video.layout import MediaLayout
from caddiej.video.segments import SegmentWindow

logger = logging.getLogger(__name__)

# Containers whose index is written at the end, and which can therefore be
# checked for having been finished. Everything CaddieJ writes is one of these.
_INDEXED_CONTAINERS = frozenset({".mp4", ".mov", ".m4v"})


@dataclass(frozen=True, slots=True)
class ClipRequest:
    """A swing worth keeping, and when it happened.

    `impact_monotonic` is CaddieJ's own clock, so it can be compared with the
    recording. `impact_at` is the same instant as wall clock, for the metadata.
    Both are estimates of when the ball was struck, which is earlier than when
    the launch monitor got round to mentioning it.
    """

    clip_id: UUID
    shot_id: UUID
    camera_id: str
    impact_monotonic: float
    impact_at: datetime
    pre_seconds: float
    post_seconds: float

    @property
    def start_monotonic(self) -> float:
        return self.impact_monotonic - self.pre_seconds

    @property
    def end_monotonic(self) -> float:
        return self.impact_monotonic + self.post_seconds


class ClipExtractor:
    """Makes one clip out of the segments covering a swing."""

    def __init__(
        self,
        layout: MediaLayout,
        binary: str = "ffmpeg",
        run_command: Run = run,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._layout = layout
        self._binary = binary
        self._run = run_command
        self._timeout = timeout_seconds

    async def extract(
        self,
        request: ClipRequest,
        window: SegmentWindow,
        impact_media: float,
        camera: CameraSpec,
        recorded_at: datetime,
    ) -> ClipRef:
        """Produce the clip, or raise `ClipExtractionError` having left nothing."""

        destination = self._layout.clip_path(
            request.impact_at, request.shot_id, request.camera_id
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._layout.temporary_path(destination, request.clip_id)
        concat_file = self._layout.concat_path(request.camera_id, request.clip_id)

        # Exact rather than approximate: the clip is whole segments, so where
        # impact sits inside it is arithmetic, not a guess about where a cut
        # landed.
        impact_offset = impact_media - window.media_start
        reference = ClipRef(
            clip_id=request.clip_id,
            shot_id=request.shot_id,
            camera_id=request.camera_id,
            path=self._layout.relative(destination),
            recorded_at=recorded_at,
            impact_at=request.impact_at,
            impact_offset_seconds=round(impact_offset, 3),
            duration_seconds=round(window.duration, 3),
            fps=camera.fps,
            width=camera.width,
            height=camera.height,
            time_quality=(
                TimeQuality.CALIBRATED
                if camera.latency_seconds
                else TimeQuality.ESTIMATED
            ),
        )

        args = concat_command(concat_file, temporary, binary=self._binary)
        try:
            concat_file.write_text(
                concat_file_contents(window.paths),
                encoding="utf-8",
                newline="\n",
            )
            result = await self._run(args, self._timeout)
            if result.returncode != 0:
                raise ClipExtractionError(
                    f"ffmpeg exited with {result.returncode}: {_tail(result.stderr)}"
                )
            _verify(temporary)

            self._write_sidecar(destination, request, reference, window, camera, args)
            # Last, so that a clip appearing means everything about it is ready.
            os.replace(temporary, destination)
        except Exception:
            _discard(temporary)
            raise
        finally:
            _discard(concat_file)

        logger.info(
            "video_clip_ready",
            extra={
                "camera_id": request.camera_id,
                "clip_id": str(request.clip_id),
                "shot_id": str(request.shot_id),
                "clip_path": str(reference.path),
                "clip_duration": reference.duration_seconds,
            },
        )
        return reference

    def _write_sidecar(
        self,
        destination: Path,
        request: ClipRequest,
        reference: ClipRef,
        window: SegmentWindow,
        camera: CameraSpec,
        args: tuple[str, ...],
    ) -> None:
        payload = {
            "clip_id": str(reference.clip_id),
            "shot_id": str(reference.shot_id),
            "camera_id": reference.camera_id,
            "camera": camera.description,
            "path": str(reference.path),
            "recorded_at": reference.recorded_at.isoformat(),
            "impact_at": reference.impact_at.isoformat(),
            "impact_offset_seconds": reference.impact_offset_seconds,
            "duration_seconds": reference.duration_seconds,
            "fps": camera.fps,
            "width": camera.width,
            "height": camera.height,
            "time_quality": reference.time_quality.value,
            "camera_latency_seconds": camera.latency_seconds,
            # Requested and captured both, because they differ: the clip starts
            # on a segment boundary, so it holds a little more than was asked
            # for. Recording only one of them would lose the difference.
            "requested_window": {
                "pre_seconds": request.pre_seconds,
                "post_seconds": request.post_seconds,
                "start": round(window.requested_start, 3),
                "end": round(window.requested_end, 3),
            },
            "captured_window": {
                "start": round(window.media_start, 3),
                "end": round(window.media_end, 3),
            },
            "segments": [segment.sequence for segment in window.segments],
            "ffmpeg": list(args),
        }
        sidecar = self._layout.sidecar_path(destination)
        temporary = sidecar.with_name(f".tmp-{reference.clip_id}.json")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, sidecar)


def new_clip_id() -> UUID:
    return uuid4()


def _verify(clip: Path) -> None:
    """Check the file is a finished video, not merely a file. Raises if not.

    Existing and being non-empty is not enough, and the difference matters
    because of what the caller does next: a clip that passes is renamed into
    place, written into a sidecar, and announced as ready. An MP4 whose write
    was interrupted — FFmpeg killed at shutdown, a disk that filled with two
    seconds to go — is a plausible size and completely unplayable, because the
    index that makes it playable is written last.

    So the test is for that index. An MP4 is a flat sequence of boxes, each a
    length and a four-character name, and the one holding the index is `moov`.
    Walking the top level costs a handful of eight-byte reads and answers
    exactly the question worth asking: did FFmpeg get to the end?
    """

    if not clip.exists():
        raise ClipExtractionError("ffmpeg produced no clip")
    size = clip.stat().st_size
    if size == 0:
        raise ClipExtractionError("ffmpeg produced an empty clip")

    if clip.suffix.lower() not in _INDEXED_CONTAINERS:
        # Some other container, which this cannot read. Non-empty is the whole
        # of what can honestly be checked.
        return

    if not _has_moov(clip, size):
        raise ClipExtractionError(
            f"ffmpeg left an unfinished clip of {size} bytes (no moov box)"
        )


def _has_moov(clip: Path, size: int) -> bool:
    """Whether a top-level `moov` box exists, by walking the box headers."""

    try:
        with clip.open("rb") as handle:
            offset = 0
            while offset < size:
                header = handle.read(8)
                if len(header) < 8:
                    return False
                length = int.from_bytes(header[:4], "big")
                name = header[4:8]
                if name == b"moov":
                    return True
                if length == 1:
                    # A 64-bit length, in the eight bytes that follow.
                    extended = handle.read(8)
                    if len(extended) < 8:
                        return False
                    length = int.from_bytes(extended, "big")
                elif length == 0:
                    # Runs to the end of the file, so there is nothing after it.
                    return False
                if length < 8:
                    # Not a length this walk can trust. Refusing to guess.
                    return False
                offset += length
                handle.seek(offset)
    except OSError:
        return False
    return False


def _tail(stderr: str, limit: int = 400) -> str:
    text = stderr.strip().replace("\n", " | ")
    return text if len(text) <= limit else f"...{text[-limit:]}"


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("video_temporary_not_deleted", extra={"clip_path": str(path)})
