"""Contact-sheet helper. No OpenAI calls and no classifier runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import testhelprunner as helper
from PIL import Image

_OLDER = "20261009T174012-8-aaaaaaaa"
_NEWER_NAME = "20261009T174012-1-ffffffff"
_OLD_RESEARCH = "20261008T191545-2-09132997"


def test_newest_unlabeled_shot_is_selected_by_capture_time(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    _shot(shots / _OLDER, started_at="2026-10-09T17:40:12.100000+00:00")
    newer = _shot(shots / _NEWER_NAME, started_at="2026-10-09T17:40:12.900000+00:00")
    _shot(shots / "not-a-shot", started_at=None, metadata=False)
    result = helper.review(shots_root=shots, labels_root=labels)
    assert result["shot_id"] == newer.name
    assert result["unlabeled_count"] == 2
    assert "Found 2 unlabeled new shots." in result["text"]
    assert f"Selected newest: {newer.name}" in result["text"]
    assert f"SHOT:\n{newer.name}" in result["text"]
    state = json.loads((labels / helper.STATE_NAME).read_text(encoding="utf-8"))
    assert state == {"shot_id": newer.name}
    assert not (labels / newer.name / "contact_label.json").exists()


def test_old_research_shots_and_labeled_shots_are_skipped(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    old = _shot(shots / _OLD_RESEARCH, started_at="2026-10-08T19:15:45+00:00")
    labeled = _shot(shots / _NEWER_NAME, started_at="2026-10-09T18:00:00+00:00")
    waiting = _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00")
    _mark_labeled(labels, labeled.name)
    result = helper.review(shots_root=shots, labels_root=labels)
    assert result["shot_id"] == waiting.name
    assert old.name not in result["text"]
    assert labeled.name not in result["shot_id"]
    assert (old / "metadata.json").is_file()
    assert (labels / labeled.name / "contact_label.json").is_file()


def test_incomplete_shots_are_skipped(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    incomplete = shots / "20261009T180000-9-incomplete"
    _shot(incomplete, started_at="2026-10-09T18:00:00+00:00", write_images=False)
    missing_replay = shots / "20261009T190000-9-noreplay"
    _shot(
        missing_replay,
        started_at="2026-10-09T19:00:00+00:00",
        replay_seconds=-1.0,
    )
    (shots / "contact_labels").mkdir(parents=True)
    (shots / "catalog_v1").mkdir(parents=True)
    ready = _shot(shots / _OLDER, started_at="2026-10-09T12:00:00+00:00")
    result = helper.review(shots_root=shots, labels_root=labels)
    assert result["shot_id"] == ready.name
    assert incomplete.name not in result["text"]
    assert (incomplete / "metadata.json").is_file()


def test_current_shot_survives_until_it_is_labeled(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    first = _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00")
    opened = helper.review(shots_root=shots, labels_root=labels)
    later = _shot(shots / _NEWER_NAME, started_at="2026-10-09T18:00:00+00:00")
    again = helper.review(shots_root=shots, labels_root=labels)
    assert opened["shot_id"] == again["shot_id"] == first.name
    assert again["continued"] is True
    assert later.name != again["shot_id"]
    state = json.loads((labels / helper.STATE_NAME).read_text(encoding="utf-8"))
    assert state == {"shot_id": first.name}


def test_contact_tick_labels_the_current_shot_and_clears_state(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    current = _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00")
    nxt = _shot(shots / _NEWER_NAME, started_at="2026-10-09T16:00:00+00:00")
    before = _capture_files(current)
    helper.review(shots_root=shots, labels_root=labels)
    text = helper.label_current(
        contact_tick=2, shots_root=shots, labels_root=labels
    )
    assert f"shot: {current.name}" in text
    assert "contact_tick: 2" in text
    assert "READY FOR NEXT SWING" in text
    saved = json.loads(
        (labels / current.name / "contact_label.json").read_text(encoding="utf-8")
    )
    assert saved["contact_tick"] == 2
    assert saved["label_source"] == "manual_visual_review"
    assert "contact_between" not in saved
    assert not (labels / helper.STATE_NAME).exists()
    assert _capture_files(current) == before
    follow = helper.review(shots_root=shots, labels_root=labels)
    assert follow["shot_id"] == nxt.name


def test_contact_between_labels_the_current_shot(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    current = _shot(shots / _NEWER_NAME, started_at="2026-10-09T18:00:00+00:00")
    helper.review(shots_root=shots, labels_root=labels)
    text = helper.label_current(
        contact_between=(1, 3), shots_root=shots, labels_root=labels
    )
    assert f"shot: {current.name}" in text
    assert "contact_between: [1, 3]" in text
    assert "READY FOR NEXT SWING" in text
    saved = json.loads(
        (labels / current.name / "contact_label.json").read_text(encoding="utf-8")
    )
    assert saved["contact_between"] == [1, 3]
    assert "contact_tick" not in saved
    assert not (labels / helper.STATE_NAME).exists()


def test_reset_clears_only_the_current_shot(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    current = _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00")
    other = _shot(shots / _NEWER_NAME, started_at="2026-10-09T16:00:00+00:00")
    before = _capture_files(current)
    other_before = _capture_files(other)
    helper.review(shots_root=shots, labels_root=labels)
    sheet = labels / current.name / "contact_sheet_01.png"
    assert sheet.is_file()
    helper.label_current(contact_tick=1, shots_root=shots, labels_root=labels)
    helper.review(shots_root=shots, labels_root=labels)
    assert (labels / helper.STATE_NAME).is_file()
    message = helper.reset(labels_root=labels)
    assert message == "Cleared the current shot."
    assert not (labels / helper.STATE_NAME).exists()
    assert (labels / current.name / "contact_label.json").is_file()
    assert sheet.is_file()
    assert _capture_files(current) == before
    assert _capture_files(other) == other_before


def test_open_commands_point_at_the_generated_sheets(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    shot = _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00", count=20)
    result = helper.review(shots_root=shots, labels_root=labels)
    generated = sorted((labels / shot.name).glob("contact_sheet_*.png"))
    assert len(generated) == len(result["open_commands"]) == 2
    for path, command in zip(result["sheets"], result["open_commands"], strict=True):
        resolved = Path(path).resolve()
        assert resolved.is_file()
        assert command == f'start "" "{resolved}"'
        assert command in result["text"]
        assert resolved in generated
    assert "python research/direct_visual_coach/testhelprunner.py --contact-tick <N>" in result["text"]
    assert (
        "python research/direct_visual_coach/testhelprunner.py --contact-between <N> <M>"
        in result["text"]
    )


def test_real_capture_layout_is_eligible_and_debug_names_the_skip(
    tmp_path: Path,
) -> None:
    shots = tmp_path / "swing_shots"
    labels = tmp_path / "labels"
    real = _real_capture(
        shots / "20261009T162747-1-2d14b82f",
        started_at="2026-10-09T16:27:47.151870+00:00",
    )
    old = _real_capture(
        shots / "20261008T200302-25-ee59697e",
        started_at="2026-10-08T20:03:02.000000+00:00",
    )
    incomplete = _real_capture(
        shots / "20261009T163021-4-f179dc48",
        started_at="2026-10-09T16:30:21.676024+00:00",
        diagnostic=False,
    )
    missing = _real_capture(
        shots / "20261009T180000-9-missing",
        started_at="2026-10-09T18:00:00+00:00",
        write_images=False,
    )
    before = _capture_files(real)
    text = helper.debug(shots_root=shots, labels_root=labels)
    assert "20261009T162747-1-2d14b82f ELIGIBLE" in text
    assert f"{old.name} SKIPPED: before validation cutoff" in text
    assert f"{incomplete.name} SKIPPED: shot has no diagnostic timeline" in text
    assert (
        f"{missing.name} SKIPPED: missing diagnostic frame "
        "diagnostic/swing_behind/frame_015.jpg"
        in text
    )
    assert not (labels / helper.STATE_NAME).exists()
    assert not (labels / real.name / "contact_label.json").exists()
    result = helper.review(shots_root=shots, labels_root=labels)
    assert result["shot_id"] == real.name
    assert _capture_files(real) == before
    assert (old / "metadata.json").is_file()
    assert (incomplete / "metadata.json").is_file()
    assert helper.CAPTURES == (
        Path(helper.__file__).resolve().parents[2] / "media" / "swing_shots"
    )


def test_no_openai_request_and_no_classifier_run(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    labels = tmp_path / "labels"
    _shot(shots / _OLDER, started_at="2026-10-09T17:00:00+00:00")
    helper.review(shots_root=shots, labels_root=labels)
    helper.label_current(contact_tick=1, shots_root=shots, labels_root=labels)
    helper.reset(labels_root=labels)
    source = Path(helper.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "openai" not in lowered
    assert "send_openai" not in source
    assert "simple_visual_classifier" not in source
    assert "rmtree" not in source
    assert "rm -r" not in source


def _real_capture(
    folder: Path,
    *,
    started_at: str,
    diagnostic: bool = True,
    write_images: bool = True,
) -> Path:
    """A swing directory in the shape written by the live capture store."""

    folder.mkdir(parents=True)
    for name in ("impact", "metrics", "swing_behind", "swing_side"):
        (folder / name).mkdir()
    (folder / "swing_evidence.jpg").write_bytes(b"sheet")
    behind = [_live_frame("swing_behind", 1, -1.3849)]
    behind.extend(
        _live_frame("swing_behind", index, (index - 15) * 0.094)
        for index in range(15, 18)
    )
    side = [_live_frame("swing_side", 1, -1.2889)]
    side.extend(
        _live_frame("swing_side", index, (index - 15) * 0.094) for index in range(15, 18)
    )
    timeline = None
    if diagnostic:
        timeline = {
            "directory": "diagnostic",
            "capture_started_at": started_at,
            "burst_started_at": started_at,
            "detected_at": started_at,
            "fps": 10.0,
            "detail": "Diagnostic only: never sent to a model or drawn on a sheet.",
            "cameras": [
                _live_camera("swing_behind", behind),
                _live_camera("swing_side", side),
            ],
        }
        if write_images:
            for camera, frames in (("swing_behind", behind), ("swing_side", side)):
                directory = folder / "diagnostic" / camera
                directory.mkdir(parents=True)
                for frame in frames:
                    image = Image.new("L", (40, 32), color=frame["capture_index"] * 8)
                    image.save(
                        directory / f"frame_{frame['capture_index']:03d}.jpg",
                        format="JPEG",
                    )
    (folder / "metadata.json").write_text(
        json.dumps(
            {
                "capture_id": "2d14b82f-443b-49ab-b86e-e70a8a24f73e",
                "trigger_id": "1",
                "strike_id": None,
                "shot_id": None,
                "coach_metrics": None,
                "detected_at": started_at,
                "started_at": started_at,
                "completed_at": started_at,
                "ok": True,
                "cameras_ready": 2,
                "selection_interval": {
                    "start_seconds": 1.902,
                    "end_seconds": 4.4,
                    "detected": True,
                    "pre_replay_frames": 14,
                },
                "configuration": {"fps": 10.0},
                "diagnostic_timeline": timeline,
                "cameras": [],
                "evidence_sheet": "swing_evidence.jpg",
                "coach_request": None,
                "provider_image_count": 0,
                "provider_images": [],
                "provider_swing_frame_count": 0,
            }
        ),
        encoding="utf-8",
    )
    return folder


def _live_camera(camera_id: str, frames: list[dict]) -> dict:
    return {
        "camera_id": camera_id,
        "source": "obs",
        "view": "dtl" if camera_id == "swing_behind" else "fo",
        "captured_frames": 51,
        "failed_frames": 0,
        "retained_frames": len(frames),
        "replay_start_seconds": 1.9018,
        "replay_start_detected": True,
        "replay_start_at": "2026-10-09T16:27:49.053670+00:00",
        "pre_replay_frames": 14,
        "replay_start_borrowed_from": None,
        "cadence": None,
        "frames": frames,
    }


def _live_frame(camera: str, index: int, replay: float) -> dict:
    return {
        "camera_id": camera,
        "capture_index": index,
        "file": f"diagnostic/{camera}/frame_{index:03d}.jpg",
        "requested_at": "2026-10-09T16:27:47.151870+00:00",
        "offset_seconds": round(1.9018 + replay, 4),
        "offset_from_strike_seconds": round(1.9018 + replay, 4),
        "offset_from_replay_start_seconds": replay,
        "width": 1002 if camera == "swing_behind" else 1272,
        "height": 1480 if camera == "swing_behind" else 1490,
        "bytes": 60005,
        "sha256": f"{camera}-{index}",
        "changed": True,
        "selected": False,
        "selected_file": None,
        "selected_phase": None,
    }


def _shot(
    folder: Path,
    *,
    started_at: str | None,
    count: int = 4,
    metadata: bool = True,
    write_images: bool = True,
    replay_seconds: float | None = None,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if not metadata:
        (folder / "readme.txt").write_text("not a shot\n", encoding="utf-8")
        return folder
    behind = []
    side = []
    for index in range(count):
        replay = index * 0.1 if replay_seconds is None else replay_seconds
        behind.append(_frame("swing_behind", index, replay))
        side.append(_frame("swing_side", index, replay))
    document: dict[str, object] = {
        "diagnostic_timeline": {
            "cameras": [
                {"camera_id": "swing_behind", "frames": behind},
                {"camera_id": "swing_side", "frames": side},
            ]
        }
    }
    if started_at is not None:
        document["started_at"] = started_at
    (folder / "metadata.json").write_text(json.dumps(document), encoding="utf-8")
    if write_images:
        for camera, frames in (("swing_behind", behind), ("swing_side", side)):
            directory = folder / "diagnostic" / camera
            directory.mkdir(parents=True)
            for frame in frames:
                image = Image.new("L", (40, 32), color=(frame["capture_index"] * 17) % 256)
                image.save(
                    directory / f"frame_{frame['capture_index']:03d}.jpg",
                    format="JPEG",
                )
    return folder


def _frame(camera: str, tick: int, replay: float) -> dict:
    return {
        "capture_index": tick,
        "offset_from_replay_start_seconds": replay,
        "file": f"diagnostic/{camera}/frame_{tick:03d}.jpg",
    }


def _mark_labeled(labels: Path, shot_id: str) -> None:
    destination = labels / shot_id
    destination.mkdir(parents=True)
    (destination / "contact_label.json").write_text(
        json.dumps(
            {
                "shot_id": shot_id,
                "contact_tick": 1,
                "label_source": "manual_visual_review",
                "created_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _capture_files(shot: Path) -> set[str]:
    return {
        path.relative_to(shot).as_posix()
        for path in shot.rglob("*")
        if path.is_file()
    }
