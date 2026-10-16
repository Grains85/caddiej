"""`caddiej --check-video-frames`, with FFmpeg replaced by a fake extractor.

The diagnostic exists to prove the production clip → stills path works on a
real machine. The tests exist to prove it names the step that failed, without
a camera, a decoder binary, or a model. Every extractor here is a fake; the
factory the diagnostic calls in production is the same one a live run uses,
and that is asserted separately.
"""

from __future__ import annotations

import json
import logging
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.coaching.frames import DEFAULT_OFFSETS, offsets_for
from caddiej.domain.media import CameraView, ClipRef, TimeQuality
from caddiej.video.diagnose import jpeg_size, probe_video_frames
from caddiej.video.fakes import FakeRun, finished_mp4
from caddiej.video.frames import FrameExtractor
from caddiej.video.layout import MediaLayout

RECORDED = datetime(2026, 8, 29, 19, 24, 14, tzinfo=UTC)
IMPACT = datetime(2026, 8, 29, 19, 24, 11, tzinfo=UTC)

# 8×6 JPEG. Enough for jpeg_size; nothing a log should ever reprint.
MINI_JPEG = (
    b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x06\x00\x08\x01\x11\x00JPEG-not-really\xff\xd9"
)


class FakeExtractor:
    def __init__(
        self,
        stills: tuple[bytes, ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[ClipRef, tuple[float, ...]]] = []
        self._stills = stills
        self._error = error

    async def stills(
        self, clip: ClipRef, offsets: list[float] | tuple[float, ...]
    ) -> tuple[bytes, ...]:
        self.calls.append((clip, tuple(offsets)))
        if self._error is not None:
            raise self._error
        if self._stills is not None:
            return self._stills
        return tuple(MINI_JPEG for _ in offsets)


def write_clip(
    root: Path,
    *,
    recorded_at: datetime = RECORDED,
    impact_at: datetime = IMPACT,
    impact_offset: float = 2.5,
    duration: float = 4.0,
    quality: TimeQuality = TimeQuality.ESTIMATED,
    camera_id: str = "tee",
    finished: bool = True,
    sidecar: bool = True,
) -> ClipRef:
    clip = ClipRef(
        clip_id=uuid4(),
        shot_id=uuid4(),
        camera_id=camera_id,
        path=Path("pending"),
        recorded_at=recorded_at,
        impact_at=impact_at,
        impact_offset_seconds=impact_offset,
        duration_seconds=duration,
        fps=60,
        width=1280,
        height=720,
        time_quality=quality,
    )
    layout = MediaLayout(root)
    destination = layout.clip_path(impact_at, clip.shot_id, camera_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(finished_mp4() if finished else b"ftyp-but-no-moov")
    clip = ClipRef(
        clip_id=clip.clip_id,
        shot_id=clip.shot_id,
        camera_id=camera_id,
        path=layout.relative(destination),
        recorded_at=recorded_at,
        impact_at=impact_at,
        impact_offset_seconds=impact_offset,
        duration_seconds=duration,
        fps=60,
        width=1280,
        height=720,
        time_quality=quality,
    )
    if sidecar:
        layout.sidecar_path(destination).write_text(
            json.dumps(
                {
                    "clip_id": str(clip.clip_id),
                    "shot_id": str(clip.shot_id),
                    "camera_id": camera_id,
                    "path": str(clip.path),
                    "recorded_at": recorded_at.isoformat(),
                    "impact_at": impact_at.isoformat(),
                    "impact_offset_seconds": impact_offset,
                    "duration_seconds": duration,
                    "fps": 60,
                    "width": 1280,
                    "height": 720,
                    "time_quality": quality.value,
                }
            ),
            encoding="utf-8",
        )
    return clip


def a_probe(root: Path, **options: object):
    defaults: dict[str, object] = {
        "media_root": root,
        "camera_view": CameraView.FACE_ON.value,
        "ffmpeg_available": lambda _binary: True,
        "extractor_factory": lambda: FakeExtractor(),
        "output_root": root / "diag",
        "now": lambda: datetime(2026, 8, 29, 19, 30, tzinfo=UTC),
    }
    defaults.update(options)
    return probe_video_frames(**defaults)  # type: ignore[arg-type]


async def test_it_extracts_the_production_coaching_stills(tmp_path: Path) -> None:
    clip = write_clip(tmp_path)
    extractor = FakeExtractor()

    probe = await a_probe(tmp_path, extractor_factory=lambda: extractor)

    assert probe.ok
    assert extractor.calls
    used, seeks = extractor.calls[0]
    assert used.shot_id == clip.shot_id
    assert seeks == offsets_for(clip, DEFAULT_OFFSETS)
    assert len(seeks) == len(DEFAULT_OFFSETS) == 4
    destination = tmp_path / "diag" / "diagnostics" / "video-frames" / "20260829-193000"
    for index in range(1, 5):
        assert (destination / f"frame-{index:02d}.jpg").read_bytes() == MINI_JPEG
    sheet = destination / "contact-sheet.html"
    assert sheet.is_file()
    html = sheet.read_text(encoding="utf-8")
    assert "frame-01.jpg" in html
    assert "address" not in html.lower()
    assert "follow-through" not in html
    assert "OpenAI calls: 0" in probe.report
    assert "Frames uploaded: 0" in probe.report
    assert "Camera view          face_on" in probe.report
    assert "estimated" in probe.report
    assert str(clip.shot_id) in probe.report


async def test_the_production_factory_is_what_builds_the_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an injected factory this is the same construction a live run
    uses. That is the whole point of the check."""

    write_clip(tmp_path)
    built: list[tuple[object, ...]] = []
    extractor = FakeExtractor()

    def factory(media_root, binary="ffmpeg", width=512):
        built.append((media_root, binary, width))
        return extractor

    monkeypatch.setattr("caddiej.video.diagnose.build_frame_extractor", factory)

    probe = await probe_video_frames(
        media_root=tmp_path,
        camera_view="down_the_line",
        ffmpeg_path="ffmpeg-custom",
        frame_width=640,
        ffmpeg_available=lambda _binary: True,
        output_root=tmp_path / "diag",
        now=lambda: datetime(2026, 8, 29, 19, 30, tzinfo=UTC),
    )

    assert probe.ok
    assert built == [(tmp_path, "ffmpeg-custom", 640)]
    assert "Camera view          down_the_line" in probe.report


async def test_the_newest_valid_clip_is_selected_not_the_newest_file(
    tmp_path: Path,
) -> None:
    older = write_clip(tmp_path, recorded_at=RECORDED)
    newer = write_clip(
        tmp_path,
        recorded_at=RECORDED.replace(minute=30),
        impact_at=IMPACT.replace(minute=30),
    )
    stray = MediaLayout(tmp_path).clip_dir(IMPACT) / "235959-ffffffff-tee.mp4"
    stray.write_bytes(finished_mp4())
    (tmp_path / older.path).touch()

    extractor = FakeExtractor()
    probe = await a_probe(tmp_path, extractor_factory=lambda: extractor)

    assert probe.ok
    assert extractor.calls[0][0].shot_id == newer.shot_id
    assert "newest valid production clip" in probe.report
    assert stray.name not in probe.report


async def test_a_newer_invalid_clip_does_not_hide_an_older_valid_one(
    tmp_path: Path,
) -> None:
    valid = write_clip(tmp_path, recorded_at=RECORDED)
    write_clip(
        tmp_path,
        recorded_at=RECORDED.replace(hour=20),
        impact_at=IMPACT.replace(hour=20),
        finished=False,
    )

    extractor = FakeExtractor()
    probe = await a_probe(tmp_path, extractor_factory=lambda: extractor)

    assert probe.ok
    assert extractor.calls[0][0].shot_id == valid.shot_id


async def test_no_usable_clip_names_what_was_looked_at(tmp_path: Path) -> None:
    stray = MediaLayout(tmp_path).clip_dir(IMPACT) / "192411-deadbeef-tee.mp4"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(finished_mp4())

    probe = await a_probe(tmp_path)

    assert not probe.ok
    assert "FAILED at clip" in probe.report
    assert str(tmp_path / "clips") in probe.report
    assert "sibling .json" in probe.report
    assert "rejected" in probe.report
    assert "no sidecar" in probe.report
    assert "VIDEO_ENABLED=true" in probe.report


async def test_a_missing_decoder_is_named(tmp_path: Path) -> None:
    write_clip(tmp_path)

    probe = await a_probe(tmp_path, ffmpeg_available=lambda _binary: False)

    assert not probe.ok
    assert "FAILED at decoder" in probe.report
    assert "VIDEO_FFMPEG_PATH" in probe.report


async def test_a_short_extractor_result_is_a_failure(tmp_path: Path) -> None:
    write_clip(tmp_path)

    probe = await a_probe(
        tmp_path, extractor_factory=lambda: FakeExtractor(stills=(MINI_JPEG,))
    )

    assert not probe.ok
    assert "FAILED at extraction" in probe.report
    assert "expected 4" in probe.report


async def test_an_extractor_that_raises_is_named(tmp_path: Path) -> None:
    write_clip(tmp_path)

    probe = await a_probe(
        tmp_path,
        extractor_factory=lambda: FakeExtractor(error=RuntimeError("unreadable")),
    )

    assert not probe.ok
    assert "FAILED at extraction" in probe.report
    assert "unreadable" in probe.report


async def test_working_stills_are_not_left_in_the_media_root(tmp_path: Path) -> None:
    write_clip(tmp_path)
    run = FakeRun(contents=MINI_JPEG)

    def factory() -> FrameExtractor:
        return FrameExtractor(MediaLayout(tmp_path), run_command=run)

    probe = await a_probe(tmp_path, extractor_factory=factory)

    assert probe.ok
    assert list((tmp_path / ".frames").glob("*.jpg")) == []
    assert run.commands
    destination = tmp_path / "diag" / "diagnostics" / "video-frames" / "20260829-193000"
    assert (destination / "frame-01.jpg").is_file()
    assert (destination / "contact-sheet.html").is_file()
    clip_jpegs = list((tmp_path / "clips").rglob("*.jpg"))
    assert clip_jpegs == []


async def test_an_unknown_camera_view_is_admitted_rather_than_inferred(
    tmp_path: Path,
) -> None:
    write_clip(tmp_path)

    probe = await a_probe(tmp_path, camera_view="side_on")

    assert probe.ok
    assert "Camera view          unknown" in probe.report
    assert "face_on" not in probe.report.split("Camera view", 1)[1][:40]


async def test_output_lands_under_the_diagnostic_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_clip(tmp_path / "media")
    monkeypatch.chdir(tmp_path)

    probe = await probe_video_frames(
        media_root=tmp_path / "media",
        ffmpeg_available=lambda _binary: True,
        extractor_factory=lambda: FakeExtractor(),
        now=lambda: datetime(2026, 8, 29, 19, 30, tzinfo=UTC),
    )

    destination = (
        tmp_path / ".caddiej" / "diagnostics" / "video-frames" / "20260829-193000"
    )
    assert probe.ok
    assert (destination / "frame-01.jpg").is_file()
    assert (destination / "contact-sheet.html").is_file()
    assert str(destination) in probe.report


async def test_image_bytes_never_reach_the_report_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_clip(tmp_path)
    marker = b"JPEG-not-really"

    with caplog.at_level(logging.DEBUG):
        probe = await a_probe(
            tmp_path,
            extractor_factory=lambda: FakeExtractor(
                stills=tuple(MINI_JPEG for _ in DEFAULT_OFFSETS)
            ),
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    logged += repr([record.__dict__ for record in caplog.records])
    assert probe.ok
    assert marker.decode() not in probe.report
    assert marker.decode() not in logged
    assert b64encode(marker).decode()[:12] not in logged
    assert b64encode(marker).decode()[:12] not in probe.report
    assert "video_check_completed" in {record.getMessage() for record in caplog.records}


async def test_no_coaching_provider_is_imported(tmp_path: Path) -> None:
    write_clip(tmp_path)
    import sys

    before = {
        name
        for name in sys.modules
        if "openai" in name or name.endswith("coaching.service")
    }
    probe = await a_probe(tmp_path)
    after = {
        name
        for name in sys.modules
        if "openai" in name or name.endswith("coaching.service")
    }

    assert probe.ok
    assert after == before


def test_jpeg_size_reads_the_sof_marker() -> None:
    assert jpeg_size(MINI_JPEG) == (8, 6)
    assert jpeg_size(b"not-a-jpeg") is None
