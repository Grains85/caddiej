"""Manual contact labels. No OpenAI calls and no classifier."""

from __future__ import annotations

import json
import re
from pathlib import Path

import contact_label
import pytest
from PIL import Image

_CAPTION = re.compile(r"^tick \d+\n\+\d+\.\d{2} s$")
_FORBIDDEN = re.compile(
    r"phase|impact|contact|fault|classifier|prediction|swing|\bP[1-9]\b",
    re.IGNORECASE,
)
_PRESERVED = "20261008T191545-2-09132997"


def test_preserved_shot_lists_every_replay_era_frame_once() -> None:
    shot = contact_label.SHOTS / _PRESERVED
    if not (shot / "metadata.json").is_file():
        pytest.skip("preserved fresh_v2 shot is not on this machine")
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    frames = contact_label.replay_frames(meta)
    ticks = [frame.tick for frame in frames]
    assert len(frames) == 37
    assert ticks == list(range(15, 52))
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == 37
    assert frames[0].replay_seconds == pytest.approx(0.0, abs=1e-4)
    assert frames[-1].replay_seconds == pytest.approx(2.4916, abs=1e-4)
    pages = contact_label.paginate(frames)
    assert [len(page) for page in pages] == [16, 16, 5]
    assert [frame.tick for frame in pages[0]] == list(range(15, 31))
    assert [frame.tick for frame in pages[1]] == list(range(31, 47))
    assert [frame.tick for frame in pages[2]] == list(range(47, 52))
    assert max(len(page) for page in pages) <= contact_label.FRAMES_PER_SHEET
    captions = [
        contact_label.frame_caption(frame.tick, frame.replay_seconds) for frame in frames
    ]
    assert captions[0] == "tick 15\n+0.00 s"
    assert captions[22] == "tick 37\n+1.79 s"
    assert all(_CAPTION.match(caption) for caption in captions)
    assert all(_FORBIDDEN.search(caption) is None for caption in captions)


def test_sheets_are_chronological_and_leave_the_picture_uncropped(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "sheet-shot", 21, phase="impact")
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    frames = contact_label.replay_frames(meta)
    assert [frame.tick for frame in frames] == list(range(21))
    sheets = contact_label.write_sheets(shot, frames, tmp_path / "sheets")
    listed = [tick for sheet in sheets for tick in sheet["ticks"]]
    assert listed == list(range(21))
    assert [len(sheet["ticks"]) for sheet in sheets] == [16, 5]
    assert all(sheet["ticks"] == sorted(sheet["ticks"]) for sheet in sheets)
    assert all(len(sheet["ticks"]) <= 16 for sheet in sheets)
    for sheet in sheets:
        for caption in sheet["captions"]:
            assert _CAPTION.match(caption)
            assert _FORBIDDEN.search(caption) is None
            assert "impact" not in caption
            assert "DTL" not in caption
    full = Image.open(sheets[0]["path"])
    partial = Image.open(sheets[1]["path"])
    try:
        assert full.size == (336, 656)
        assert partial.size == (336, 352)
        assert (sheets[0]["width"], sheets[0]["height"]) == full.size
    finally:
        full.close()
        partial.close()
    assert not (tmp_path / "sheets" / "contact_label.json").exists()


def test_review_without_a_contact_argument_writes_no_label(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "plain-shot", 4)
    result = contact_label.run(
        shot.name,
        shots_root=shot.parent,
        labels_root=tmp_path / "labels",
    )
    assert result["contact_label"] is None
    assert result["frame_count"] == 4
    destination = tmp_path / "labels" / shot.name
    assert (destination / "contact_sheet_01.png").is_file()
    assert not (destination / "contact_label.json").exists()


def test_exact_contact_label_is_only_the_temporal_fields(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "exact-shot", 8)
    result = contact_label.run(
        shot.name,
        contact_tick=3,
        shots_root=shot.parent,
        labels_root=tmp_path / "labels",
    )
    document = result["contact_label"]
    assert document is not None
    assert set(document) == {"shot_id", "contact_tick", "label_source", "created_at"}
    assert document["shot_id"] == shot.name
    assert document["contact_tick"] == 3
    assert document["label_source"] == "manual_visual_review"
    assert "T" in document["created_at"]
    stored = json.loads((tmp_path / "labels" / shot.name / "contact_label.json").read_text())
    assert stored == document
    assert "contact_between" not in stored


def test_interval_contact_label_keeps_both_endpoints(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "interval-shot", 8)
    result = contact_label.run(
        shot.name,
        contact_between=(2, 5),
        shots_root=shot.parent,
        labels_root=tmp_path / "labels",
    )
    document = result["contact_label"]
    assert document is not None
    assert set(document) == {
        "shot_id",
        "contact_between",
        "label_source",
        "created_at",
    }
    assert document["contact_between"] == [2, 5]
    assert "contact_tick" not in document
    assert document["label_source"] == "manual_visual_review"


def test_contact_arguments_are_mutually_exclusive(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "both-shot", 8)
    with pytest.raises(SystemExit, match="only one"):
        contact_label.run(
            shot.name,
            contact_tick=3,
            contact_between=(2, 5),
            shots_root=shot.parent,
            labels_root=tmp_path / "labels",
        )
    assert not (tmp_path / "labels" / shot.name / "contact_label.json").exists()
    with pytest.raises(SystemExit):
        contact_label.main([shot.name, "--contact-tick", "3", "--contact-between", "2", "5"])


def test_unknown_ticks_and_a_reversed_interval_are_rejected(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "reject-shot", 8)
    labels = tmp_path / "labels"
    with pytest.raises(SystemExit, match="not in the replay-era timeline"):
        contact_label.run(
            shot.name, contact_tick=41, shots_root=shot.parent, labels_root=labels
        )
    with pytest.raises(SystemExit, match="not in the replay-era timeline"):
        contact_label.run(
            shot.name,
            contact_between=(2, 41),
            shots_root=shot.parent,
            labels_root=labels,
        )
    with pytest.raises(SystemExit, match="later tick"):
        contact_label.run(
            shot.name,
            contact_between=(5, 5),
            shots_root=shot.parent,
            labels_root=labels,
        )
    with pytest.raises(SystemExit, match="later tick"):
        contact_label.run(
            shot.name,
            contact_between=(5, 2),
            shots_root=shot.parent,
            labels_root=labels,
        )
    assert not (labels / shot.name / "contact_label.json").exists()


def test_a_label_file_cannot_carry_notes_or_both_contact_fields(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "blind-shot", 8)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    known = {frame.tick for frame in contact_label.replay_frames(meta)}
    path = tmp_path / "contact_label.json"
    path.write_text(
        json.dumps(
            {
                "shot_id": shot.name,
                "contact_tick": 3,
                "label_source": "manual_visual_review",
                "created_at": "2026-10-09T12:00:00+00:00",
                "notes": "early release",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="notes"):
        contact_label.load_contact_label(path, shot.name, known)
    path.write_text(
        json.dumps(
            {
                "shot_id": shot.name,
                "contact_tick": 3,
                "contact_between": [2, 5],
                "label_source": "manual_visual_review",
                "created_at": "2026-10-09T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one"):
        contact_label.load_contact_label(path, shot.name, known)



def _write_shot(folder: Path, count: int, *, phase: str | None = None) -> Path:
    behind = [_frame("swing_behind", index, index * 0.1, phase) for index in range(count)]
    side = [_frame("swing_side", index, index * 0.1, phase) for index in range(count)]
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps(
            {
                "diagnostic_timeline": {
                    "cameras": [
                        {"camera_id": "swing_behind", "frames": behind},
                        {"camera_id": "swing_side", "frames": side},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    for camera, frames in (("swing_behind", behind), ("swing_side", side)):
        directory = folder / "diagnostic" / camera
        directory.mkdir(parents=True)
        width = 40 if camera == "swing_behind" else 48
        for frame in frames:
            image = Image.new("L", (width, 32), color=(frame["capture_index"] * 17) % 256)
            image.save(directory / f"frame_{frame['capture_index']:03d}.jpg", format="JPEG")
    return folder


def _frame(camera: str, tick: int, replay: float, phase: str | None) -> dict:
    return {
        "capture_index": tick,
        "offset_from_replay_start_seconds": replay,
        "offset_seconds": round(2.0 + replay, 4),
        "bytes": 60_000,
        "sha256": f"{camera}-{tick}",
        "file": f"diagnostic/{camera}/frame_{tick:03d}.jpg",
        "width": 40 if camera == "swing_behind" else 48,
        "height": 32,
        "selected_phase": phase,
    }
