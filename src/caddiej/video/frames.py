"""Pulling stills out of a finished clip.

The seam between filming and looking at what was filmed, and it is deliberately
one method wide: offsets in, JPEG bytes out. Video does not learn that a language
model exists, and coaching does not learn that FFmpeg does.

**Nothing here raises.** A frame that cannot be decoded is a reason to coach from
the numbers, which is a complete answer, so a failure returns fewer frames — or
none — and says so in the log. Every offset is independent, so one unreadable
position does not cost the other three.

Each still is written to a temporary file, read, and deleted. Asking FFmpeg for
bytes on a pipe would save the write, but four small files that are removed
immediately are much easier to reason about than four concurrent pipes, and this
runs once per shot rather than once per frame of video.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from caddiej.domain.media import ClipRef
from caddiej.video.ffmpeg import Run, run, still_command
from caddiej.video.layout import MediaLayout

logger = logging.getLogger(__name__)


class FrameExtractor:
    """Turns a clip and a list of offsets into JPEG stills."""

    def __init__(
        self,
        layout: MediaLayout,
        binary: str = "ffmpeg",
        run_command: Run = run,
        width: int = 512,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._layout = layout
        self._binary = binary
        self._run = run_command
        self._width = width
        self._timeout = timeout_seconds

    async def stills(
        self, clip: ClipRef, offsets: Sequence[float]
    ) -> tuple[bytes, ...]:
        """Stills at those offsets into the clip, in order. Never raises."""

        source = self._layout.root / clip.path
        if not source.exists():
            logger.warning(
                "video_frames_source_missing",
                extra={"clip_id": str(clip.clip_id), "clip_path": str(clip.path)},
            )
            return ()

        self._layout.prepare_frames()
        frames = []
        for index, offset in enumerate(offsets):
            data = await self._still(clip, source, offset, index)
            if data:
                frames.append(data)

        logger.info(
            "video_frames_extracted",
            extra={
                "clip_id": str(clip.clip_id),
                "shot_id": str(clip.shot_id),
                "video_frames": len(frames),
            },
        )
        return tuple(frames)

    async def _still(
        self, clip: ClipRef, source: Path, offset: float, index: int
    ) -> bytes | None:
        destination = self._layout.still_path(clip.clip_id, index)
        args = still_command(
            source,
            offset,
            destination,
            width=self._width,
            binary=self._binary,
        )
        try:
            result = await self._run(args, self._timeout)
            if result.returncode != 0 or not destination.exists():
                logger.warning(
                    "video_frame_not_extracted",
                    extra={
                        "clip_id": str(clip.clip_id),
                        "clip_offset": round(offset, 3),
                        "video_detail": result.stderr[-200:] if result.stderr else "",
                    },
                )
                return None
            return destination.read_bytes() or None
        except TimeoutError:
            logger.warning(
                "video_frame_timed_out",
                extra={"clip_id": str(clip.clip_id), "clip_offset": round(offset, 3)},
            )
            return None
        except OSError as error:
            logger.warning(
                "video_frame_failed",
                extra={"clip_id": str(clip.clip_id), "clip_offset": round(offset, 3)},
                exc_info=error,
            )
            return None
        finally:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "video_frame_file_kept", extra={"clip_path": str(destination)}
                )
