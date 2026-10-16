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


@dataclass(frozen=True, slots=True)
class DiscoveredClip:
    """A finished production clip, reconstructed from the sidecar beside it."""

    clip: ClipRef
    media_path: Path
    sidecar_path: Path


@dataclass(frozen=True, slots=True)
class RejectedClip:
    """A file under the clips tree that is not a usable production clip."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ClipCatalog:
    """Everything under the media root that looks like a swing clip."""

    clips_root: Path
    accepted: tuple[DiscoveredClip, ...]
    rejected: tuple[RejectedClip, ...]

    @property
    def newest(self) -> DiscoveredClip | None:
        """The most recently recorded valid clip, or None if there is none.

        `recorded_at` from the sidecar, not the file's mtime: a copied library
        keeps the time the swing was filmed, and mtime would quietly prefer
        whichever file a backup last touched.
        """

        return self.accepted[0] if self.accepted else None


def clip_from_sidecar(sidecar: Path) -> ClipRef:
    """The clip the sidecar describes, or raise if it is not one CaddieJ wrote.

    The sidecar exists so a clip stays interpretable after the process that
    filmed it has gone. Coaching never reads one today — it is handed a
    `ClipRef` on the bus — but anything that has to find a swing after the fact
    has to start here, because the MP4 does not know when impact was.
    """

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClipExtractionError(f"sidecar is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise ClipExtractionError("sidecar is not an object")

    try:
        return ClipRef(
            clip_id=UUID(str(payload["clip_id"])),
            shot_id=UUID(str(payload["shot_id"])),
            camera_id=str(payload["camera_id"]),
            path=Path(str(payload["path"])),
            recorded_at=_when(payload["recorded_at"]),
            impact_at=_when(payload["impact_at"]),
            impact_offset_seconds=float(payload["impact_offset_seconds"]),
            duration_seconds=float(payload["duration_seconds"]),
            fps=float(payload["fps"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            time_quality=TimeQuality(str(payload.get("time_quality", "estimated"))),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ClipExtractionError(f"sidecar is incomplete: {error}") from error


def discover_clips(layout: MediaLayout) -> ClipCatalog:
    """Every production clip under the media root, newest first.

    A clip is a pair: an MP4 and the sidecar that explains it. An MP4 alone is
    not enough — nothing in the container says where impact falls — and a
    sidecar alone is a description of a file that is not there. Both, or it is
    rejected rather than guessed at.
    """

    clips_root = layout.root / "clips"
    accepted: list[DiscoveredClip] = []
    rejected: list[RejectedClip] = []
    if not clips_root.is_dir():
        return ClipCatalog(clips_root=clips_root, accepted=(), rejected=())

    for media in sorted(clips_root.rglob("*.mp4")):
        if not media.is_file() or media.name.startswith("."):
            continue
        sidecar = layout.sidecar_path(media)
        if not sidecar.is_file():
            rejected.append(
                RejectedClip(media, "no sidecar — not a CaddieJ swing clip")
            )
            continue
        try:
            clip = clip_from_sidecar(sidecar)
            _verify(media)
        except ClipExtractionError as error:
            rejected.append(RejectedClip(media, str(error)))
            continue
        accepted.append(
            DiscoveredClip(clip=clip, media_path=media, sidecar_path=sidecar)
        )

    accepted.sort(
        key=lambda item: (item.clip.recorded_at, item.media_path), reverse=True
    )
    return ClipCatalog(
        clips_root=clips_root,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def _when(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing")
    return datetime.fromisoformat(value)


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
