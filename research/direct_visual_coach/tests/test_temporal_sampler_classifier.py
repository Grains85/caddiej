"""Sampler frames into the simplified classifier. No OpenAI calls."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

import direct_visual_test as coach
import fault_catalog as catalog
import pytest
import simple_visual_classifier as simple
import temporal_sampler_classifier as runner
from PIL import Image

HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / "temporal_sampler_classifier.py"
SAMPLER_SOURCE = HERE / "temporal_sampler.py"
SHOTS = (
    "20261008T191451-1-93738c0a",
    "20261008T191545-2-09132997",
    "20261008T191626-3-694d0a52",
    "20261008T191913-6-345eaca1",
    "20261008T191951-7-5b29909f",
)


def test_module_does_not_touch_the_sampler_or_the_contact_anchor() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert "import temporal_sampler" not in text
    assert "simple_visual_classifier_contact_anchor" not in text
    assert "import openai" not in text
    assert "coach.send_openai" in text


def test_request_uses_the_simplified_classifier_unchanged(tmp_path: Path) -> None:
    catalog_bytes = Path(catalog.__file__).read_bytes()
    sampler_bytes = SAMPLER_SOURCE.read_bytes()
    frames = _write_selection(tmp_path, "shot-a")
    request = runner.build_request(frames)
    assert Path(catalog.__file__).read_bytes() == catalog_bytes
    assert SAMPLER_SOURCE.read_bytes() == sampler_bytes
    assert request.system == simple.SYSTEM
    assert request.instructions == simple.INSTRUCTIONS
    assert request.schema == simple.schema()
    assert request.schema_name == simple.SCHEMA_NAME
    assert request.reasoning_effort == coach.REASONING_EFFORT
    assert request.max_output_tokens == coach.MAX_OUTPUT_TOKENS
    assert len(simple.simplified_catalog()) == 26
    assert simple.simplified_catalog_json() in request.evidence
    assert all(image.detail == coach.DETAIL == "high" for image in request.images)
    assert len(request.images) == 24
    assert [image.data for image in request.images] == [frame.data for frame in frames]


def test_preamble_states_spacing_without_a_selection_reason(tmp_path: Path) -> None:
    frames = _write_selection(tmp_path, "shot-b")
    request = runner.build_request(frames)
    assert "They are chronological." in request.evidence
    assert "relative_timestamp_seconds" in request.evidence
    assert "Frame spacing is intentionally non-uniform." in request.evidence
    assert (
        "A smaller time gap means the local sampler chose denser temporal coverage."
        in request.evidence
    )
    assert "seconds_since_previous_image" in request.evidence
    audit = runner.request_audit(request, frames)
    assert audit == {name: False for name in audit}
    head = request.evidence.split("\nVISUAL FAULT CATALOG\n", maxsplit=1)[0]
    assert "capture_tick 0" in head
    assert "capture_tick 23" in head


def test_original_jpegs_are_sent_and_the_sheet_is_not(tmp_path: Path) -> None:
    frames = _write_selection(tmp_path, "shot-c")
    request = runner.build_request(frames)
    for frame, image in zip(frames, request.images, strict=True):
        assert image.data == frame.path.read_bytes()
        assert image.data[:2] == b"\xff\xd8"
        assert frame.path.name != "selected_sheet.jpg"
        assert "selected_sheet" not in image.label
        assert "selection_reason" not in image.label
    sheet = tmp_path / "selected_sheet.jpg"
    sheet.write_bytes(frames[0].data)
    broken = _manifest(tmp_path, "shot-sheet", frames)
    broken[0]["path"] = str(sheet)
    broken[0]["file"] = sheet.name
    _dump(tmp_path, "shot-sheet", broken)
    with pytest.raises(ValueError, match="selected_sheet"):
        runner.load_selected_frames("shot-sheet", tmp_path)


def test_selection_must_be_twenty_four_unique_chronological_frames(
    tmp_path: Path,
) -> None:
    frames = _write_selection(tmp_path, "shot-d")
    short = _manifest(tmp_path, "shot-short", frames[:23])
    _dump(tmp_path, "shot-short", short)
    with pytest.raises(ValueError, match="expected 24"):
        runner.load_selected_frames("shot-short", tmp_path)

    duplicate = _manifest(tmp_path, "shot-dup", frames)
    duplicate[1]["sha256"] = duplicate[0]["sha256"]
    duplicate[1]["path"] = duplicate[0]["path"]
    _dump(tmp_path, "shot-dup", duplicate)
    with pytest.raises(ValueError, match="not unique"):
        runner.load_selected_frames("shot-dup", tmp_path)

    backward = _manifest(tmp_path, "shot-back", frames)
    backward[2]["capture_tick"] = 1
    _dump(tmp_path, "shot-back", backward)
    with pytest.raises(ValueError, match="chronological"):
        runner.load_selected_frames("shot-back", tmp_path)


def test_dry_run_writes_the_request_and_does_not_call_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_request):
        raise AssertionError("OpenAI was called")

    monkeypatch.setattr(coach, "send_openai", boom)
    _write_selection(tmp_path / "sampler", "shot-e")
    report = runner.execute(
        "shot-e",
        send=False,
        sampler_root=tmp_path / "sampler",
        results_root=tmp_path / "results",
    )
    assert report["sent"] is False
    assert report["image_count"] == 24
    assert report["catalog_fault_count"] == 26
    assert report["model"] == coach.MODEL
    assert report["reasoning_effort"] == coach.REASONING_EFFORT
    assert report["detail"] == "high"
    assert report["contact_anchor_supplied"] is False
    stored = json.loads(
        (tmp_path / "results" / "shot-e" / "request.json").read_text(encoding="utf-8")
    )
    assert stored["schema"] == simple.schema()
    assert len(stored["images"]) == 24
    assert stored["images"][0]["path"].endswith("frame_000.jpg")
    assert not (tmp_path / "results" / "shot-e" / "reply.json").exists()
    text = runner.format_summary(report)
    assert "contact_anchor_in_request: no" in text
    assert "original_source_images_sent: yes" in text
    assert "selected_sheet_sent: no" in text


def test_send_stores_the_reply_separately_and_validates_with_the_simplified_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = _write_selection(tmp_path / "sampler", "shot-f")
    ticks = [frame.capture_tick for frame in frames]

    def complete(_request):
        return _Reply(_no_fault(ticks))

    monkeypatch.setattr(
        coach, "send_openai", lambda _request: (_ for _ in ()).throw(AssertionError())
    )
    report = runner.execute(
        "shot-f",
        send=True,
        sampler_root=tmp_path / "sampler",
        results_root=tmp_path / "results",
        complete=complete,
    )
    assert report["sent"] is True
    assert report["status"] == "no_proven_fault"
    assert report["validation_problems"] == []
    reply = json.loads(
        (tmp_path / "results" / "shot-f" / "reply.json").read_text(encoding="utf-8")
    )
    assert reply["payload"]["status"] == "no_proven_fault"
    assert "evidence" not in reply
    request = json.loads(
        (tmp_path / "results" / "shot-f" / "request.json").read_text(encoding="utf-8")
    )
    assert "payload" not in request


def test_results_cannot_overwrite_the_sampler_or_the_simplified_classifier(
    tmp_path: Path,
) -> None:
    _write_selection(tmp_path / "sampler", "shot-g")
    with pytest.raises(RuntimeError, match="cannot overwrite"):
        runner.execute(
            "shot-g",
            sampler_root=tmp_path / "sampler",
            results_root=runner.SAMPLER_RESULTS,
        )
    with pytest.raises(RuntimeError, match="cannot overwrite"):
        runner.execute(
            "shot-g",
            sampler_root=tmp_path / "sampler",
            results_root=simple.RESULTS,
        )


def test_archived_selections_pass_the_input_audit() -> None:
    catalog_bytes = Path(catalog.__file__).read_bytes()
    sampler_bytes = SAMPLER_SOURCE.read_bytes()
    for shot_id in SHOTS:
        frames = runner.load_selected_frames(shot_id)
        request = runner.build_request(frames)
        assert len(frames) == 24
        assert [frame.capture_tick for frame in frames] == sorted(
            frame.capture_tick for frame in frames
        )
        assert runner.request_audit(request, frames) == {
            name: False
            for name in (
                "contact_anchor",
                "p1_p9",
                "motion_scores",
                "optical_flow",
                "phase_labels_supplied",
                "selected_sheet_sent",
                "selection_reason_sent",
            )
        }
        assert all(frame.path.name != "selected_sheet.jpg" for frame in frames)
        assert all(
            image.data == frame.data
            for image, frame in zip(request.images, frames, strict=True)
        )
    assert Path(catalog.__file__).read_bytes() == catalog_bytes
    assert SAMPLER_SOURCE.read_bytes() == sampler_bytes


def _write_selection(root: Path, shot_id: str) -> list[runner.SelectedSource]:
    directory = root / shot_id
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(24):
        path = directory / f"frame_{index:03d}.jpg"
        path.write_bytes(_jpeg(index))
        rows.append(
            {
                "order": index,
                "capture_tick": index,
                "elapsed_seconds": round(1.0 + index * 0.1, 4),
                "path": str(path),
                "width": 32,
                "height": 24,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
            }
        )
    _dump(root, shot_id, rows)
    return runner.load_selected_frames(shot_id, root)


def _manifest(
    root: Path, shot_id: str, frames: list[runner.SelectedSource]
) -> list[dict]:
    del shot_id
    return [
        {
            "order": frame.order,
            "capture_tick": frame.capture_tick,
            "elapsed_seconds": frame.relative_timestamp_seconds,
            "path": str(frame.path),
            "width": frame.width,
            "height": frame.height,
            "sha256": frame.recorded_sha256,
        }
        for frame in frames
    ]


def _dump(root: Path, shot_id: str, rows: list[dict]) -> None:
    directory = root / shot_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"shot_id": shot_id, "frames": rows}
    (directory / "selected_frames.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _jpeg(index: int) -> bytes:
    image = Image.new("RGB", (32, 24), (0, 0, 0))
    for column in range(index + 1):
        for row in range(8):
            image.putpixel((column, row), (255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _no_fault(ticks: list[int]) -> dict:
    return {
        "status": "no_proven_fault",
        "handedness": {
            "side": "right",
            "confidence": 0.5,
            "visual_basis": "The frames show which side is closer to the target.",
        },
        "temporal_orientation": [
            {
                "capture_tick": tick,
                "state": "uncertain",
                "confidence": 0.4,
                "visual_basis": "The frames do not settle the direction.",
            }
            for tick in ticks
        ],
        "fault_candidates": [],
    }


class _Reply:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.model = coach.MODEL
        self.latency_seconds = 0.2
        self.input_tokens = 10
        self.output_tokens = 4
        self.reasoning_tokens = 0
