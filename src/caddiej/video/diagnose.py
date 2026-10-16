"""Proving, against a recorded swing, that CaddieJ can pull the coaching stills.

There are two very different reasons coaching sends no frames: the model path
never asked for them, or the clip/decoder/offset path cannot produce them. From
the bay those look identical, so this walks the video half on demand — find a
production clip, reconstruct its `ClipRef` from the sidecar, apply the same
offsets coaching uses, extract the stills through the same FFmpeg extractor —
and says which step stopped working.

It inspects footage that is already on disk. It does not start a camera, a
coaching provider, or the rest of CaddieJ. A diagnostic that invented its own
timing or decoder would pass while the one the coach uses stayed broken.

Prose rather than JSON, because it is read by a person at a terminal in the
same session as `--check-speech`.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from caddiej.coaching.frames import DEFAULT_OFFSETS, offsets_for
from caddiej.domain.media import CameraView, ClipRef, TimeQuality
from caddiej.video.clips import ClipCatalog, discover_clips
from caddiej.video.ffmpeg import is_available
from caddiej.video.frames import build_frame_extractor
from caddiej.video.jpeg_size import jpeg_size
from caddiej.video.layout import MediaLayout

logger = logging.getLogger(__name__)


class StillExtractor(Protocol):
    async def stills(
        self, clip: ClipRef, offsets: Sequence[float]
    ) -> tuple[bytes, ...]: ...


ExtractorFactory = Callable[[], StillExtractor]


@dataclass(frozen=True, slots=True)
class VideoFramesProbe:
    """What happened, and whether the operator has to look at the files."""

    ok: bool
    report: str


async def probe_video_frames(
    *,
    media_root: Path | str,
    camera_view: str = CameraView.UNKNOWN.value,
    ffmpeg_path: str = "ffmpeg",
    frame_width: int = 512,
    requested_offsets: Sequence[float] = DEFAULT_OFFSETS,
    output_root: Path | str | None = None,
    now: Callable[[], datetime] | None = None,
    ffmpeg_available: Callable[[str], bool] = is_available,
    extractor_factory: ExtractorFactory | None = None,
) -> VideoFramesProbe:
    """Extract the production coaching stills from the newest usable clip.

    Never logs image bytes. Never talks to a model. `VIDEO_ENABLED` is not
    consulted: the extractor does not need a camera, only a clip that is
    already on disk.
    """

    root = Path(media_root)
    view = _camera_view(camera_view)
    stamp = (now or (lambda: datetime.now(UTC)))()
    destination = _destination(output_root, stamp)
    lines = [
        "Swing-video frames check — production clip, production stills",
        f"  Media root           {root}",
        f"  Clips                {root / 'clips'}",
        f"  Camera view          {view.value}",
        f"  Decoder              {ffmpeg_path}",
        "",
    ]
    logger.info(
        "video_check_started",
        extra={
            "video_root": str(root),
            "video_camera_view": view.value,
        },
    )

    layout = MediaLayout(root)
    catalog = discover_clips(layout)
    logger.info(
        "video_check_clip_found",
        extra={
            "video_root": str(catalog.clips_root),
            "video_frames": len(catalog.accepted),
        },
    )
    chosen = catalog.newest
    if chosen is None:
        return _no_clip(lines, catalog)

    clip = chosen.clip
    lines.extend(
        [
            f"  selected             {chosen.media_path}",
            f"  sidecar              {chosen.sidecar_path}",
            "  why                  newest valid production clip "
            f"(recorded_at {clip.recorded_at.isoformat()})",
            f"  shot_id              {clip.shot_id}",
            f"  camera_id            {clip.camera_id}",
            f"  duration             {clip.duration_seconds:g}s",
            f"  impact offset        {clip.impact_offset_seconds:g}s into the clip",
            f"  impact at            {clip.impact_at.isoformat()}",
            f"  impact timing        {clip.time_quality.value}",
            "",
        ]
    )
    logger.info(
        "video_check_clip_selected",
        extra={
            "clip_id": str(clip.clip_id),
            "shot_id": str(clip.shot_id),
            "clip_path": str(clip.path),
            "clip_duration": clip.duration_seconds,
            "camera_id": clip.camera_id,
        },
    )
    logger.info(
        "video_check_reference_time",
        extra={
            "clip_id": str(clip.clip_id),
            "clip_offset": clip.impact_offset_seconds,
            "clip_time_quality": clip.time_quality.value,
        },
    )

    if not ffmpeg_available(ffmpeg_path):
        return _failed(
            lines,
            "decoder",
            f"{ffmpeg_path} was not found",
            "Frame extraction uses the same FFmpeg binary a live run does. "
            "Install it and put it on PATH, or set VIDEO_FFMPEG_PATH.",
        )

    seeks = offsets_for(clip, requested_offsets)
    extractor = (
        extractor_factory()
        if extractor_factory is not None
        else build_frame_extractor(root, binary=ffmpeg_path, width=frame_width)
    )
    try:
        stills = await extractor.stills(clip, seeks)
    except Exception as error:
        return _failed(lines, "extraction", error, "")

    if len(stills) != len(seeks):
        return _failed(
            lines,
            "extraction",
            f"decoder returned {len(stills)} stills, expected {len(seeks)}",
            "The production extractor never raises: a missing or unreadable "
            "frame is a shorter tuple. That is not enough for this check.",
        )

    try:
        destination.mkdir(parents=True, exist_ok=True)
        frame_paths = _write_frames(destination, stills)
        sheet = _write_contact_sheet(
            destination, clip, requested_offsets, seeks, frame_paths
        )
    except OSError as error:
        return _failed(lines, "output", error, "")

    for index, (requested, seek, path) in enumerate(
        zip(requested_offsets, seeks, frame_paths, strict=True), start=1
    ):
        applied = seek - clip.impact_offset_seconds
        size = jpeg_size(stills[index - 1])
        size_text = f"{size[0]} × {size[1]}" if size is not None else "unknown"
        lines.append(f"  frame {index} of {len(seeks)}")
        lines.append(f"    requested            {_seconds(requested)} from impact")
        if clip.time_quality is TimeQuality.ESTIMATED and applied != requested:
            lines.append(
                f"    applied              {_seconds(applied)} from impact "
                "(widened; impact timing is estimated)"
            )
        lines.append(f"    seek                 {_seconds(seek)} into the clip")
        lines.append("    decoded timestamp    not returned by FFmpeg still extraction")
        lines.append(f"    size                 {size_text}")
        lines.append(f"    file                 {path}")
        logger.info(
            "video_check_frame_extracted",
            extra={
                "clip_id": str(clip.clip_id),
                "clip_offset": seek,
                "video_frames": index,
            },
        )

    lines.extend(
        [
            "",
            f"  contact sheet        {sheet}",
            f"  output               {destination}",
            "",
            "OpenAI calls: 0",
            "Frames uploaded: 0",
            "",
            "These stills are time samples, not detected swing positions. Do not",
            "read one as address, the top, or impact unless something else has",
            "actually found that position.",
        ]
    )
    logger.info(
        "video_check_contact_sheet_written",
        extra={"clip_path": str(sheet)},
    )
    logger.info(
        "video_check_completed",
        extra={
            "clip_id": str(clip.clip_id),
            "video_frames": len(stills),
            "video_camera_view": view.value,
        },
    )
    return VideoFramesProbe(ok=True, report="\n".join(lines))


def _camera_view(value: str) -> CameraView:
    try:
        return CameraView(value)
    except ValueError:
        return CameraView.UNKNOWN


def _destination(output_root: Path | str | None, when: datetime) -> Path:
    root = Path(output_root) if output_root is not None else Path(".caddiej")
    stamp = when.strftime("%Y%m%d-%H%M%S")
    return (root / "diagnostics" / "video-frames" / stamp).resolve()


def _write_frames(destination: Path, stills: Sequence[bytes]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for index, data in enumerate(stills, start=1):
        path = destination / f"frame-{index:02d}.jpg"
        path.write_bytes(data)
        paths.append(path)
    return tuple(paths)


def _write_contact_sheet(
    destination: Path,
    clip: ClipRef,
    requested: Sequence[float],
    seeks: Sequence[float],
    frames: Sequence[Path],
) -> Path:
    """A single HTML page of the stills, labelled only with time offsets."""

    cells: list[str] = []
    for index, (offset, seek, path) in enumerate(
        zip(requested, seeks, frames, strict=True), start=1
    ):
        applied = seek - clip.impact_offset_seconds
        cells.append(
            "<figure>"
            f'<img src="{html.escape(path.name)}" alt="frame {index}">'
            f"<figcaption>frame {index}  {_seconds(applied)}</figcaption>"
            "</figure>"
        )
    page = destination / "contact-sheet.html"
    page.write_text(
        "<!doctype html>\n"
        "<meta charset='utf-8'>\n"
        "<title>CaddieJ swing-video frames</title>\n"
        "<style>"
        "body{font:14px/1.4 sans-serif;margin:1.5rem;background:#111;color:#eee}"
        "main{display:flex;flex-wrap:wrap;gap:1rem}"
        "figure{margin:0}"
        "img{display:block;max-width:320px;height:auto;background:#000}"
        "figcaption{margin-top:.4rem}"
        "</style>\n"
        "<p>Time offsets relative to the sidecar's impact. Positions have not "
        "been detected.</p>\n"
        f"<main>\n{''.join(cells)}\n</main>\n",
        encoding="utf-8",
    )
    return page


def _seconds(value: float) -> str:
    return f"{value:+.2f} s"


def _no_clip(lines: list[str], catalog: ClipCatalog) -> VideoFramesProbe:
    lines.append("  FAILED at clip: no usable production swing clip")
    lines.append("")
    lines.append(f"  looked in           {catalog.clips_root}")
    lines.append("  expected             clips/YYYY-MM-DD/*.mp4 with a sibling .json")
    lines.append(
        "  not searched         ProTee Shot Video, OBS captures, arbitrary MP4s"
    )
    if catalog.rejected:
        lines.append("  rejected")
        for item in catalog.rejected[:8]:
            lines.append(f"    {item.path} — {item.reason}")
        if len(catalog.rejected) > 8:
            lines.append(f"    … {len(catalog.rejected) - 8} more")
    lines.extend(
        [
            "",
            "CaddieJ only analyses clips it recorded itself, under VIDEO_MEDIA_ROOT.",
            "Enable VIDEO_ENABLED=true, confirm FFmpeg is on PATH, and hit a shot",
            "(or run GSPRO_MODE=simulate) so a clip and sidecar are written.",
        ]
    )
    logger.warning(
        "video_check_failed",
        extra={"video_detail": "no usable production swing clip"},
    )
    return VideoFramesProbe(ok=False, report="\n".join(lines))


def _failed(
    lines: list[str], step: str, error: object, advice: str
) -> VideoFramesProbe:
    text = str(error)
    lines.append(f"  FAILED at {step}: {text}")
    if advice:
        lines.extend(["", advice])
    lines.extend(["", "OpenAI calls: 0", "Frames uploaded: 0"])
    logger.warning(
        "video_check_failed", extra={"video_detail": f"{step}: {text}"[:300]}
    )
    return VideoFramesProbe(ok=False, report="\n".join(lines))
