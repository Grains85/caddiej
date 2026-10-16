"""Simplified visual classifier. No OpenAI calls. Catalog V1 stays frozen."""

from __future__ import annotations

import json
from pathlib import Path

import direct_visual_test as dv
import experiment_runner as matrix
import fault_catalog as catalog
import pytest
import simple_visual_classifier as simple
from PIL import Image

_FORBIDDEN = (
    "required_evidence",
    "insufficient_evidence",
    "allowed_temporal_states",
    "baseline_temporal_states",
    "requires_baseline",
    "min_fault_observations",
    "must_include_states",
    "requires_handedness",
    "confusable_with",
    "mutually_exclusive_with",
    "upstream_candidates",
    "proven only when",
    "omit unless",
)


def test_projection_is_the_existing_catalog_and_nothing_else() -> None:
    source_ids = [fault.id for fault in catalog.CATALOG]
    projected = simple.simplified_catalog()
    assert len(source_ids) == 26
    assert [row["id"] for row in projected] == source_ids
    assert set(projected[0]) == set(simple.SENT_FIELDS)
    for row in projected:
        assert set(row) == set(simple.SENT_FIELDS)
        assert row["id"] in catalog.FAULTS_BY_ID


def test_catalog_source_keeps_the_strict_fields() -> None:
    source = Path(catalog.__file__).read_bytes()
    simple.simplified_catalog()
    assert Path(catalog.__file__).read_bytes() == source
    sent = catalog.classifier_catalog()
    assert set(sent[0]) == set(catalog.CLASSIFIER_FIELDS)
    assert "required_evidence" in sent[0]
    assert "coaching" not in sent[0]
    assert catalog.CATALOG[0].required_evidence


def test_withheld_rules_are_not_in_the_projection_or_the_prompt() -> None:
    blob = simple.simplified_catalog_json()
    for field in simple.WITHHELD_FIELDS:
        assert f'"{field}"' not in blob
    prompt = simple.SYSTEM + simple.INSTRUCTIONS
    for phrase in _FORBIDDEN:
        assert phrase not in prompt
    assert "No Face-On frame is attached" in simple.INSTRUCTIONS
    assert "Down-the-Line and Face-On" not in prompt
    assert "not coaching the golfer" in simple.SYSTEM


def test_schema_asks_for_ticks_and_visual_basis_only() -> None:
    body = simple.schema()
    candidate = body["properties"]["fault_candidates"]["items"]
    assert candidate["required"] == ["fault_id", "confidence", "evidence"]
    evidence = candidate["properties"]["evidence"]["items"]
    assert evidence["required"] == ["capture_tick", "visual_basis"]
    assert "view" not in evidence["properties"]
    encoded = json.dumps(body)
    assert "why_required_evidence_is_met" not in encoded
    assert "alternatives_considered" not in encoded
    assert candidate["properties"]["fault_id"]["enum"] == [
        fault.id for fault in catalog.CATALOG
    ]


def test_dtl_large_selection_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shot-select", 30)
    seen: list[dict] = []
    real = dv.plan_selection

    def spy(meta, **kwargs):
        seen.append(kwargs)
        return real(meta, **kwargs)

    monkeypatch.setattr(dv, "plan_selection", spy)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = simple.select_dtl_large(meta, shot)
    again = simple.select_dtl_large(meta, shot)
    ticks = [moment.capture_tick for moment in plan.moments]
    assert seen[0]["count"] == matrix.moment_budget("DTL", "LARGE") == 24
    assert seen[0]["views"] == matrix.views_for("DTL") == ("DTL",)
    assert seen[0]["shot"] == shot
    assert ticks == [moment.capture_tick for moment in again.moments]
    assert ticks[0] == 0
    assert ticks[-1] == 29
    assert len(ticks) == 24
    assert all(
        len(moment.frames) == 1 and moment.frames[0].view == "DTL"
        for moment in plan.moments
    )


def test_request_sends_only_dtl_images(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shot-dtl", 30)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    moments = simple.select_dtl_large(meta, shot).moments
    loaded = dv.load_frames(shot, moments)
    request = simple.build_request(moments, loaded)
    assert request.system == simple.SYSTEM
    assert request.instructions == simple.INSTRUCTIONS
    assert request.reasoning_effort == dv.REASONING_EFFORT
    assert len(request.images) == 24
    assert {item.frame.view for item in loaded} == {"DTL"}
    assert all(image.detail == dv.DETAIL for image in request.images)
    assert all(image.label.startswith("DTL ") for image in request.images)
    assert "FO " not in " ".join(image.label for image in request.images)
    assert "No Face-On frame is attached" in request.evidence
    assert simple.simplified_catalog_json() in request.evidence
    for field in (
        "required_evidence",
        "insufficient_evidence",
        "allowed_temporal_states",
        "requires_baseline",
        "confusable_with",
        "mutually_exclusive_with",
        "upstream_candidates",
        "coaching",
    ):
        assert f'"{field}"' not in request.evidence


def test_unsent_tick_is_rejected_and_a_supplied_tick_is_kept() -> None:
    ticks = {15, 16}
    ordered = sorted(ticks)
    rejected = _classified(ordered, evidence_tick=99)
    assert any(
        "was not sent" in problem for problem in simple.validate_reply(rejected, ticks)
    )
    assert simple.validate_reply(_classified(ordered, evidence_tick=16), ticks) == []
    assert simple.validate_reply(_no_fault(ordered), ticks) == []


def test_orientation_covers_every_tick_once_and_stays_forward() -> None:
    ticks = {15, 16, 17}
    missing = _no_fault([15, 16])
    assert any(
        "missing" in problem for problem in simple.validate_reply(missing, ticks)
    )
    duplicated = _no_fault([15, 16, 16])
    assert any(
        "duplicated" in problem
        for problem in simple.validate_reply(duplicated, {15, 16})
    )
    backward = _no_fault([15, 16], states={15: "moving_toward_ball", 16: "setup"})
    assert any(
        "regresses" in problem for problem in simple.validate_reply(backward, {15, 16})
    )
    uncertain = _no_fault(
        [15, 16, 17],
        states={15: "moving_away", 16: "uncertain", 17: "moving_toward_ball"},
    )
    assert simple.validate_reply(uncertain, {15, 16, 17}) == []


def test_strict_catalog_validator_is_not_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("strict Catalog V1 validator was applied")

    monkeypatch.setattr(dv, "validate", boom)
    monkeypatch.setattr(dv, "interpret", boom)
    fault = catalog.FAULTS_BY_ID["BACKSWING_LEAD_ARM_COLLAPSED"]
    assert fault.requires_handedness
    assert fault.preferred_views == ("FO",)
    ticks = {15, 33}
    payload = _classified(
        sorted(ticks),
        fault_id=fault.id,
        evidence_tick=33,
        state_at_evidence="setup",
        side="uncertain",
    )
    assert simple.validate_reply(payload, ticks) == []
    strict = _strict_candidate(fault, tick=33)
    sent = {("DTL", 15), ("DTL", 33)}
    monkeypatch.undo()
    problems = dv.validate(strict, sent)
    assert problems
    assert any("was not supplied" in problem for problem in problems)
    assert any("handedness is uncertain" in problem for problem in problems)


def test_review_sheet_is_a_four_column_grid_at_original_size(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "sheet-shot", 24)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    loaded = dv.load_frames(shot, simple.select_dtl_large(meta, shot).moments)
    assert len(loaded) == 24
    path = tmp_path / "review_sheet.png"
    simple.write_review_sheet(path, loaded)
    sheet = Image.open(path)
    source = Image.open(shot / "diagnostic" / "swing_behind" / "frame_000.jpg")
    cell_w, cell_h = source.size
    row_h = simple._SHEET_LABEL_H + cell_h
    assert sheet.size == (
        simple._SHEET_MARGIN * 2 + 4 * cell_w + 3 * simple._SHEET_GAP,
        simple._SHEET_MARGIN * 2 + 6 * row_h + 5 * simple._SHEET_GAP,
    )
    for index in (0, 1, 4, 23):
        src = Image.open(
            shot / "diagnostic" / "swing_behind" / f"frame_{index:03d}.jpg"
        ).convert("RGB")
        column = index % simple.SHEET_COLUMNS
        row = index // simple.SHEET_COLUMNS
        x = simple._SHEET_MARGIN + column * (cell_w + simple._SHEET_GAP)
        y = simple._SHEET_MARGIN + row * (row_h + simple._SHEET_GAP)
        y += simple._SHEET_LABEL_H
        crop = sheet.crop((x, y, x + src.width, y + src.height))
        assert crop.size == src.size
        assert crop.tobytes() == src.tobytes()


def test_dry_run_makes_no_openai_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-dry", 12)
    calls: list[object] = []

    def boom(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("OpenAI was called")

    monkeypatch.setattr(dv, "send_openai", boom)
    monkeypatch.setattr(dv, "validate", boom)
    monkeypatch.setattr(simple, "RESULTS", tmp_path / "simple_visual_classifier")
    report = simple.execute(shot.name, shots_root=shot.parent)
    assert calls == []
    assert report["sent"] is False
    assert report["temporal_moments"] == 12
    assert report["image_count"] == 12
    out = Path(report["result_directory"])
    assert (out / "request.json").is_file()
    assert (out / "report.json").is_file()
    assert (out / "review_sheet.png").is_file()
    assert not (out / "reply.json").exists()
    stored = json.loads((out / "request.json").read_text(encoding="utf-8"))
    assert stored["system"] == simple.SYSTEM
    assert stored["instructions"] == simple.INSTRUCTIONS
    assert stored["model"] == dv.MODEL
    assert stored["reasoning_effort"] == dv.REASONING_EFFORT
    assert [image["view"] for image in stored["images"]] == ["DTL"] * 12


def test_results_cannot_overwrite_catalog_or_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-iso", 8)
    catalog_root = tmp_path / "catalog_v1"
    matrix_root = tmp_path / "camera_budget_matrix"
    catalog_root.mkdir()
    matrix_root.mkdir()
    catalog_sentinel = catalog_root / "report.json"
    matrix_sentinel = matrix_root / "comparison.json"
    catalog_sentinel.write_text('{"keep": true}\n', encoding="utf-8")
    matrix_sentinel.write_text('{"keep": true}\n', encoding="utf-8")
    monkeypatch.setattr(dv, "RESULTS", catalog_root)
    monkeypatch.setattr(matrix, "RESULTS", matrix_root)
    with pytest.raises(RuntimeError):
        simple.execute(shot.name, shots_root=shot.parent, results_root=catalog_root)
    with pytest.raises(RuntimeError):
        simple.execute(shot.name, shots_root=shot.parent, results_root=matrix_root)
    simple_root = tmp_path / "simple_visual_classifier"
    report = simple.execute(shot.name, shots_root=shot.parent, results_root=simple_root)
    assert Path(report["result_directory"]) == simple_root / shot.name
    assert catalog_sentinel.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert matrix_sentinel.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert simple.RESULTS != dv.RESULTS
    assert simple.RESULTS != matrix.RESULTS
    assert simple.RESULTS.name == "simple_visual_classifier"


def test_cli_does_not_send_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-cli", 8)
    written: list[object] = []
    monkeypatch.setattr(dv, "SHOTS", shot.parent)
    monkeypatch.setattr(simple, "RESULTS", tmp_path / "simple_visual_classifier")
    monkeypatch.setattr(
        dv, "send_openai", lambda *_args, **_kwargs: written.append(True)
    )
    simple.main([shot.name])
    captured = capsys.readouterr().out
    assert "SYSTEM" in captured
    assert simple.SYSTEM.strip() in captured
    assert written == []
    assert not list((tmp_path / "simple_visual_classifier").rglob("reply.json"))


def _classified(
    ticks: list[int],
    *,
    fault_id: str = "STEEP_DELIVERY",
    evidence_tick: int = 15,
    state_at_evidence: str = "moving_toward_ball",
    side: str = "right",
) -> dict:
    states = {tick: "setup" for tick in ticks}
    states[evidence_tick] = state_at_evidence
    return {
        "status": "classified",
        "handedness": _hand(side),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [
            {
                "fault_id": fault_id,
                "confidence": 0.82,
                "evidence": [
                    {
                        "capture_tick": evidence_tick,
                        "visual_basis": "The shaft is steep beside the forearm.",
                    }
                ],
            }
        ],
    }


def _strict_candidate(fault: catalog.VisualFault, *, tick: int) -> dict:
    return {
        "status": "classified",
        "handedness": _hand("uncertain"),
        "temporal_orientation": _orient([15, tick], {15: "setup", tick: "setup"}),
        "fault_candidates": [
            {
                "fault_id": fault.id,
                "confidence": 0.82,
                "evidence": [
                    {
                        "view": "DTL",
                        "capture_tick": tick,
                        "observation": "The lead arm is folded.",
                    }
                ],
                "why_required_evidence_is_met": ["visible"]
                * len(fault.required_evidence),
                "alternatives_considered": [],
            }
        ],
    }


def _no_fault(ticks: list[int], states: dict[int, str] | None = None) -> dict:
    return {
        "status": "no_proven_fault",
        "handedness": _hand(),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [],
    }


def _hand(side: str = "right") -> dict:
    return {
        "side": side,
        "confidence": 0.9,
        "visual_basis": "The address frame shows which side is closer to the target.",
    }


def _orient(ticks: list[int], states: dict[int, str] | None = None) -> list[dict]:
    chosen = states or {}
    return [
        {
            "capture_tick": tick,
            "state": chosen.get(tick, "setup"),
            "confidence": 0.8,
            "visual_basis": "The hands show the direction.",
        }
        for tick in ticks
    ]


def _write_shot(folder: Path, count: int) -> Path:
    times = [(index, index * 0.1) for index in range(count)]
    behind = [_frame("swing_behind", tick, replay) for tick, replay in times]
    side = [_frame("swing_side", tick, replay) for tick, replay in times]
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
            image = Image.new(
                "L", (width, 32), color=(frame["capture_index"] * 17) % 256
            )
            image.save(
                directory / f"frame_{frame['capture_index']:03d}.jpg", format="JPEG"
            )
    return folder


def _frame(camera: str, tick: int, replay: float) -> dict:
    return {
        "capture_index": tick,
        "offset_from_replay_start_seconds": replay,
        "offset_seconds": round(2.0 + replay, 4),
        "bytes": 60_000,
        "sha256": f"{camera}-{tick}",
        "file": f"diagnostic/{camera}/frame_{tick:03d}.jpg",
        "width": 40 if camera == "swing_behind" else 48,
        "height": 32,
        "selected_phase": None,
    }
