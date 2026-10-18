"""The capture-timing diagnostic: every screenshot kept, nothing else moved.

Three real swings are to be captured with every Swing Behind and Swing Side
screenshot on disk, so P1-P8 can be read off the whole timeline before the
capture rate is changed. That is only worth anything if the diagnostic watches
the capture rather than changing it: the same OBS requests, the same selected
frames, the same sheet, the same request. And a dry run that spares the paid
call must spare only the call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.unit.capture_bay import BEHIND, SIDE, Bay, aimed

from caddiej.config import Settings
from caddiej.domain.events import ShotDetected
from caddiej.domain.media import SwingCapture
from caddiej.swing_capture.store import (
    DIAGNOSTIC_DIRECTORY,
    DIAGNOSTIC_KEY,
    EVIDENCE_KEEP,
    EVIDENCE_KEEP_MAX,
    METADATA_NAME,
    retention_limit,
)

BODY = ("swing_behind", "swing_side")


def metadata(capture: SwingCapture) -> dict[str, Any]:
    return json.loads((capture.directory / METADATA_NAME).read_text("utf-8"))


def diagnostic_camera(capture: SwingCapture, camera_id: str) -> dict[str, Any]:
    block = metadata(capture)[DIAGNOSTIC_KEY]
    return next(c for c in block["cameras"] if c["camera_id"] == camera_id)


def production_camera(capture: SwingCapture, camera_id: str) -> dict[str, Any]:
    return next(c for c in metadata(capture)["cameras"] if c["camera_id"] == camera_id)


def selected_bytes(capture: SwingCapture) -> dict[str, list[bytes]]:
    return {
        camera.camera_id: [
            (capture.directory / frame.path).read_bytes() for frame in camera.frames
        ]
        for camera in capture.cameras
    }


async def two_bays(tmp_path: Path) -> tuple[Bay, SwingCapture, Bay, SwingCapture]:
    plain = Bay(tmp_path / "plain")
    diagnostic = Bay(tmp_path / "diagnostic", diagnostic_timeline_shots=3)
    return plain, await plain.shot(), diagnostic, await diagnostic.shot()


# --- every screenshot already taken is kept ----------------------------------


class TestEveryCapturedFrameIsRetained:
    @pytest.mark.parametrize(
        ("camera_id", "source"), [(BODY[0], BEHIND), (BODY[1], SIDE)]
    )
    async def test_every_screenshot_of_the_camera_is_on_disk(
        self, tmp_path, camera_id: str, source: str
    ) -> None:
        bay = Bay(tmp_path, diagnostic_timeline_shots=3)
        capture = await bay.shot()

        camera = diagnostic_camera(capture, camera_id)
        timeline = production_camera(capture, camera_id)["timeline"]
        files = sorted((capture.directory / DIAGNOSTIC_DIRECTORY / camera_id).iterdir())

        assert bay.obs.taken[source] == camera["captured_frames"]
        assert camera["retained_frames"] == camera["captured_frames"] == len(timeline)
        assert len(files) == len(timeline)
        for entry, trace in zip(camera["frames"], timeline, strict=True):
            data = (capture.directory / entry["file"]).read_bytes()
            assert hashlib.sha256(data).hexdigest()[:16] == trace["sha256"]
            assert len(data) == trace["bytes"]

    async def test_frame_order_and_timing_are_preserved(self, tmp_path) -> None:
        capture = await Bay(tmp_path, diagnostic_timeline_shots=3).shot()
        block = metadata(capture)[DIAGNOSTIC_KEY]

        assert block["burst_started_at"] is not None
        assert block["capture_started_at"] == metadata(capture)["started_at"]
        for camera_id in BODY:
            camera = diagnostic_camera(capture, camera_id)
            production = production_camera(capture, camera_id)
            start = production["replay_interval"]["start_seconds"]
            assert camera["replay_start_seconds"] == pytest.approx(start, abs=1e-3)
            assert camera["replay_start_detected"] is True
            assert camera["replay_start_at"] is not None
            frames = camera["frames"]
            assert [f["capture_index"] for f in frames] == list(
                range(1, len(frames) + 1)
            )
            assert [f["file"] for f in frames] == [
                f"diagnostic/{camera_id}/frame_{n:03d}.jpg"
                for n in range(1, len(frames) + 1)
            ]
            offsets = [f["offset_seconds"] for f in frames]
            assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
            assert offsets == pytest.approx(
                [t["offset_seconds"] for t in production["timeline"]], abs=1e-3
            )
            requested = [f["requested_at"] for f in frames]
            assert requested == sorted(requested)
            for entry in frames:
                assert entry["camera_id"] == camera_id
                assert entry["offset_from_replay_start_seconds"] == pytest.approx(
                    entry["offset_seconds"] - camera["replay_start_seconds"], abs=1e-3
                )
                assert (entry["width"], entry["height"]) == (640, 360)

    async def test_the_selected_frames_are_marked_in_the_timeline(
        self, tmp_path
    ) -> None:
        capture = await Bay(tmp_path, diagnostic_timeline_shots=3).shot()

        for camera_id in BODY:
            frames = diagnostic_camera(capture, camera_id)["frames"]
            kept = production_camera(capture, camera_id)["frames"]
            marked = [f for f in frames if f["selected"]]
            assert [f["capture_index"] for f in marked] == [
                k["captured_index"] for k in kept
            ]
            assert [f["selected_file"] for f in marked] == [k["file"] for k in kept]
            assert [f["selected_phase"] for f in marked] == [k["phase"] for k in kept]

    async def test_the_static_impact_panel_is_not_part_of_it(self, tmp_path) -> None:
        capture = await Bay(tmp_path, diagnostic_timeline_shots=3).shot()

        cameras = [c["camera_id"] for c in metadata(capture)[DIAGNOSTIC_KEY]["cameras"]]
        assert cameras == list(BODY)
        assert not (capture.directory / DIAGNOSTIC_DIRECTORY / "impact").exists()

    async def test_off_by_default_and_production_writes_nothing_extra(
        self, tmp_path
    ) -> None:
        assert Settings.model_construct().swing_capture_diagnostic_timeline_shots == 0
        capture = await Bay(tmp_path).shot()

        assert DIAGNOSTIC_KEY not in metadata(capture)
        assert not (capture.directory / DIAGNOSTIC_DIRECTORY).exists()


# --- and nothing that production does moves ------------------------------------


class TestProductionIsUntouched:
    async def test_no_additional_obs_screenshot_request(self, tmp_path) -> None:
        plain, _, diagnostic, _ = await two_bays(tmp_path)

        assert diagnostic.obs.taken == plain.obs.taken

    async def test_selected_evidence_is_unchanged(self, tmp_path) -> None:
        _, plain, _, diagnostic = await two_bays(tmp_path)

        assert diagnostic.cameras == plain.cameras
        assert selected_bytes(diagnostic) == selected_bytes(plain)
        assert diagnostic.metrics is not None and plain.metrics is not None
        assert replace(
            diagnostic.metrics, captured_at=None, offset_seconds=None
        ) == replace(plain.metrics, captured_at=None, offset_seconds=None)
        stripped = {
            key: value
            for key, value in metadata(diagnostic).items()
            if key != DIAGNOSTIC_KEY
        }
        plain_meta = metadata(plain)
        assert stripped["configuration"]["retain_diagnostic_timeline"] is True
        assert plain_meta["configuration"]["retain_diagnostic_timeline"] is False
        for key in ("cameras", "selection_interval", "configuration", "cameras_ready"):
            left = stripped[key]
            right = plain_meta[key]
            if key == "configuration":
                left = {
                    name: value
                    for name, value in left.items()
                    if name != "retain_diagnostic_timeline"
                }
                right = {
                    name: value
                    for name, value in right.items()
                    if name != "retain_diagnostic_timeline"
                }
            assert left == right, key

    async def test_phase_selection_is_unchanged(self, tmp_path) -> None:
        _, plain, _, diagnostic = await two_bays(tmp_path)

        for camera_id in (*BODY, "impact"):
            assert aimed(diagnostic, camera_id) == aimed(plain, camera_id)


# --- bounded, and inside the ordinary lifecycle ------------------------------


class TestBoundedAndPruned:
    def test_timeline_count_is_bounded_and_twenty_is_allowed(self) -> None:
        timeline = Settings.model_fields["swing_capture_diagnostic_timeline_shots"]
        keep = Settings.model_fields["swing_capture_evidence_keep"]
        assert any(getattr(item, "le", None) == EVIDENCE_KEEP_MAX for item in timeline.metadata)
        assert any(getattr(item, "le", None) == EVIDENCE_KEEP_MAX for item in keep.metadata)
        assert any(getattr(item, "ge", None) == EVIDENCE_KEEP for item in keep.metadata)
        Settings(SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS=20)  # type: ignore[call-arg]
        Settings(SWING_CAPTURE_EVIDENCE_KEEP=20)  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            Settings(SWING_CAPTURE_DIAGNOSTIC_TIMELINE_SHOTS=EVIDENCE_KEEP_MAX + 1)  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            Settings(SWING_CAPTURE_EVIDENCE_KEEP=2)  # type: ignore[call-arg]
        assert retention_limit() == EVIDENCE_KEEP
        assert retention_limit(EVIDENCE_KEEP, 20) == 20
        assert retention_limit(20, 0) == 20

    async def test_only_the_configured_number_of_shots_keep_their_timeline(
        self, tmp_path
    ) -> None:
        bay = Bay(tmp_path, diagnostic_timeline_shots=2)

        first = await bay.shot("81")
        second = await bay.shot("82")
        third = await bay.shot("83")

        assert DIAGNOSTIC_KEY in metadata(first)
        assert DIAGNOSTIC_KEY in metadata(second)
        assert DIAGNOSTIC_KEY not in metadata(third)
        assert not (third.directory / DIAGNOSTIC_DIRECTORY).exists()

    async def test_the_shots_are_pruned_like_any_other(self, tmp_path) -> None:
        bay = Bay(tmp_path, diagnostic_timeline_shots=3)
        shots = [await bay.shot(trigger) for trigger in ("81", "82", "83", "84")]

        root = shots[-1].directory.parent
        kept = sorted(path.name for path in root.iterdir())
        assert len(kept) == EVIDENCE_KEEP
        assert not shots[0].directory.exists(), "its diagnostic went with it"
        for shot in shots[1:3]:
            assert (shot.directory / DIAGNOSTIC_DIRECTORY).is_dir()
        assert not (shots[3].directory / DIAGNOSTIC_DIRECTORY).exists()
        media = bay.latest.parent
        stray = [
            path
            for path in media.rglob(DIAGNOSTIC_DIRECTORY)
            if root not in path.parents and bay.latest not in path.parents
        ]
        assert stray == [], "no separate diagnostic store"

    async def test_newest_shot_still_wins_and_a_superseded_one_uses_no_slot(
        self, tmp_path
    ) -> None:
        bay = Bay(tmp_path, diagnostic_timeline_shots=2)

        await bay.service.on_shot_detected(
            ShotDetected(trigger_id="72", source="protee_log")
        )
        first = bay.service._task  # noqa: SLF001
        await asyncio.sleep(0)
        await bay.service.on_shot_detected(
            ShotDetected(trigger_id="73", source="protee_log")
        )
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.gather(bay.service._task, return_exceptions=True)  # noqa: SLF001

        captures = bay.captures()
        assert [c.trigger_id for c in captures] == ["73"]
        assert len(list(captures[0].directory.parent.iterdir())) == 1
        assert DIAGNOSTIC_KEY in metadata(captures[0])
        later = await bay.shot("74")
        assert DIAGNOSTIC_KEY in metadata(later), "the superseded strike kept its slot"
        assert DIAGNOSTIC_KEY not in metadata(await bay.shot("75"))


class TestItSaysSo:
    async def test_enabled_retained_and_finished_are_logged(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        bay = Bay(tmp_path, diagnostic_timeline_shots=1)
        bay.service.start()
        capture = await bay.shot()

        def named(event: str) -> list[Any]:
            return [r for r in caplog.records if r.getMessage() == event]

        (enabled,) = named("swing_capture_diagnostic_timeline_enabled")
        assert enabled.swing_capture_diagnostic_shots == 1
        (retained,) = named("swing_capture_diagnostic_timeline_retained")
        assert retained.swing_capture_diagnostic_complete is True
        assert retained.swing_capture_diagnostic_shots == 0
        assert retained.swing_capture_directory == str(
            capture.directory / DIAGNOSTIC_DIRECTORY
        )
        for camera_id in BODY:
            count = diagnostic_camera(capture, camera_id)["captured_frames"]
            assert f"{camera_id}={count}/{count} captured" in (
                retained.swing_capture_detail
            )
        assert len(named("swing_capture_diagnostic_timeline_finished")) == 1

    async def test_production_logs_none_of_it(self, tmp_path, caplog) -> None:
        caplog.set_level("INFO")
        bay = Bay(tmp_path)
        bay.service.start()
        await bay.shot()

        assert not [
            r for r in caplog.records if "diagnostic_timeline" in r.getMessage()
        ]
