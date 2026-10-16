"""Manual contact labels and the classifier's contact-source order. No OpenAI calls."""

from __future__ import annotations

import json
import re
from pathlib import Path

import contact_label
import direct_visual_test as dv
import pytest
import simple_visual_classifier_contact_anchor as anchor
from PIL import Image

_CAPTION = re.compile(r"^tick \d+\n\+\d+\.\d{2} s$")
_FORBIDDEN = re.compile(
    r"phase|impact|contact|fault|classifier|prediction|swing|\bP[1-9]\b",
    re.IGNORECASE,
)
_PRESERVED = "20261008T191545-2-09132997"


def test_preserved_shot_lists_every_replay_era_frame_once() -> None:
    shot = dv.SHOTS / _PRESERVED
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
    with pytest.raises(SystemExit):
        anchor.main([shot.name, "--contact-tick", "3", "--contact-between", "2", "5"])


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


def test_resolution_order_is_cli_then_file_then_preserved_fixture(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "ordered-shot", 8)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    labels = tmp_path / "labels"
    contact_label.write_contact_label(
        shot.name,
        contact_label.replay_frames(meta),
        labels / shot.name,
        contact_tick=3,
        created_at="2026-10-09T12:00:00+00:00",
    )
    from_cli = anchor.resolve_contact(
        shot.name, meta, contact_tick=6, labels_root=labels
    )
    assert from_cli.kind == "tick"
    assert from_cli.tick == 6
    assert from_cli.source == "cli"
    from_interval = anchor.resolve_contact(
        shot.name, meta, contact_between=(2, 5), labels_root=labels
    )
    assert from_interval.kind == "between"
    assert from_interval.tick is None
    assert from_interval.between == (2, 5)
    assert from_interval.source == "cli"
    from_file = anchor.resolve_contact(shot.name, meta, labels_root=labels)
    assert from_file.tick == 3
    assert from_file.source == "contact_label"
    preserved = _write_shot(tmp_path / "shots" / _PRESERVED, 8)
    preserved_meta = json.loads((preserved / "metadata.json").read_text(encoding="utf-8"))
    fixture = anchor.resolve_contact(
        preserved.name, preserved_meta, labels_root=tmp_path / "empty-labels"
    )
    assert fixture == anchor.preserved_fixture()
    assert fixture.tick == 41
    assert fixture.source == "preserved_fixture"
    with pytest.raises(SystemExit, match="no contact label"):
        anchor.resolve_contact(shot.name, meta, labels_root=tmp_path / "empty-labels")


def test_a_new_shot_without_a_label_fails_before_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "unlabeled-shot", 8)
    calls: list[object] = []

    def spy(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("request was built")

    monkeypatch.setattr(anchor, "build_request", spy)
    monkeypatch.setattr(dv, "send_openai", spy)
    with pytest.raises(SystemExit, match="no contact label"):
        anchor.execute(
            shot.name,
            shots_root=shot.parent,
            results_root=tmp_path / "classifier",
            labels_root=tmp_path / "labels",
        )
    assert calls == []
    assert not (tmp_path / "classifier" / shot.name / "request.json").exists()


def test_fixture_tick_is_not_applied_to_another_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "other-shot", 8)
    calls: list[object] = []
    monkeypatch.setattr(
        anchor, "build_request", lambda *_args, **_kwargs: calls.append(True)
    )
    with pytest.raises(SystemExit):
        anchor.execute(
            shot.name,
            shots_root=shot.parent,
            results_root=tmp_path / "classifier",
            labels_root=tmp_path / "labels",
        )
    assert calls == []
    assert anchor.preserved_fixture().tick == 41
    assert shot.name != anchor.PRESERVED_SHOT_ID


def test_exact_contact_semantics_stay_on_either_side_of_the_tick() -> None:
    ticks = {4, 7, 9}
    anchor_at = anchor.ContactAnchor("tick", 7, None, "cli")
    before = _no_fault([4, 7, 9], {4: "after_strike", 7: "after_strike", 9: "after_strike"})
    problems = anchor.validate_reply(before, ticks, anchor_at)
    assert any("capture_tick 4" in problem and "after_strike is invalid" in problem for problem in problems)
    assert not any("capture_tick 7" in problem for problem in problems)
    on_contact = _no_fault(
        [4, 7, 9], {4: "moving_toward_ball", 7: "moving_toward_ball", 9: "after_strike"}
    )
    assert anchor.validate_reply(on_contact, ticks, anchor_at) == []
    after = _no_fault(
        [4, 7, 9], {4: "setup", 7: "after_strike", 9: "moving_toward_ball"}
    )
    problems = anchor.validate_reply(after, ticks, anchor_at)
    assert any("capture_tick 9" in problem and "must be after_strike" in problem for problem in problems)
    assert not any("capture_tick 4" in problem for problem in problems)
    text = anchor_at.text()
    assert "lower than 7 is before club-ball contact" in text
    assert "higher than 7 is after club-ball contact" in text
    assert anchor.exact_anchor_text(41) == anchor.CONTACT_ANCHOR_TEXT


def test_interval_bounds_leave_the_interior_unassigned() -> None:
    bounds = anchor.ContactAnchor("between", None, (10, 20), "cli")
    ticks = {10, 15, 20}
    at_start = _no_fault(
        [10, 15, 20],
        {10: "after_strike", 15: "after_strike", 20: "after_strike"},
    )
    problems = anchor.validate_reply(at_start, ticks, bounds)
    assert any("capture_tick 10" in problem and "after_strike is invalid" in problem for problem in problems)
    assert not any("capture_tick 15" in problem for problem in problems)
    before_end = _no_fault(
        [10, 15, 20],
        {10: "setup", 15: "moving_toward_ball", 20: "moving_toward_ball"},
    )
    problems = anchor.validate_reply(before_end, ticks, bounds)
    assert any("capture_tick 20" in problem and "must be after_strike" in problem for problem in problems)
    assert not any("capture_tick 15" in problem for problem in problems)
    interior_strike = _no_fault(
        [10, 15, 20], {10: "setup", 15: "after_strike", 20: "after_strike"}
    )
    interior_approach = _no_fault(
        [10, 15, 20], {10: "setup", 15: "moving_toward_ball", 20: "after_strike"}
    )
    assert anchor.validate_reply(interior_strike, ticks, bounds) == []
    assert anchor.validate_reply(interior_approach, ticks, bounds) == []
    text = bounds.text()
    assert (
        "The club-ball contact occurs after capture_tick 10 and before capture_tick 20."
        in text
    )
    assert "around capture_tick" not in text
    assert "capture_tick 15" not in text
    assert bounds.tick is None
    assert bounds.between == (10, 20)


def test_interval_request_is_a_dry_run_and_is_not_collapsed_to_a_midpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "between-shot", 8)
    calls: list[object] = []

    def boom(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("OpenAI was called")

    monkeypatch.setattr(dv, "send_openai", boom)
    report = anchor.execute(
        shot.name,
        contact_between=(2, 6),
        shots_root=shot.parent,
        results_root=tmp_path / "classifier",
        labels_root=tmp_path / "labels",
    )
    assert calls == []
    assert report["sent"] is False
    assert report["contact_anchor_tick"] is None
    assert report["contact_between"] == [2, 6]
    assert report["contact_source"] == "cli"
    stored = json.loads(
        (Path(report["result_directory"]) / "request.json").read_text(encoding="utf-8")
    )
    text = stored["contact_anchor_text"]
    assert (
        "The club-ball contact occurs after capture_tick 2 and before capture_tick 6."
        in text
    )
    assert stored["contact_anchor_tick"] is None
    assert stored["contact_between"] == [2, 6]
    assert "around capture_tick" not in text
    assert "capture_tick 4" not in text
    assert stored["system"] == anchor.base.SYSTEM
    source = Path(contact_label.__file__).read_text(encoding="utf-8")
    assert "openai" not in source.lower()
    assert "send_openai" not in source


def _no_fault(ticks: list[int], states: dict[int, str]) -> dict:
    return {
        "status": "no_proven_fault",
        "handedness": {
            "side": "right",
            "confidence": 0.9,
            "visual_basis": "The address frame shows which side is closer to the target.",
        },
        "temporal_orientation": [
            {
                "capture_tick": tick,
                "state": states[tick],
                "confidence": 0.8,
                "visual_basis": "The hands show the direction.",
            }
            for tick in ticks
        ],
        "fault_candidates": [],
    }


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
