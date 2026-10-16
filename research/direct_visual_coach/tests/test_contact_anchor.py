"""Contact-anchor experiment. No OpenAI calls. Prior results stay put."""

from __future__ import annotations

import json
import re
from pathlib import Path

import direct_visual_test as dv
import experiment_runner as matrix
import fault_catalog as catalog
import pytest
import simple_visual_classifier as base
import simple_visual_classifier_contact_anchor as anchor
from PIL import Image

_EXPECTED_TICKS = [
    15,
    16,
    17,
    18,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    31,
    33,
    35,
    37,
    40,
    42,
    44,
    47,
    49,
    51,
]
_P_LABEL = re.compile(r"\bP[1-9]\b")


def test_contact_anchor_is_tick_41() -> None:
    assert anchor.CONTACT_ANCHOR_TICK == 41
    assert anchor.PRESERVED_SHOT_ID == "20261008T191545-2-09132997"
    assert "lower than 41 is before club-ball contact" in anchor.CONTACT_ANCHOR_TEXT
    assert "higher than 41 is after club-ball contact" in anchor.CONTACT_ANCHOR_TEXT


def test_selector_is_the_unchanged_dtl_large_selector(
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
    plan = base.select_dtl_large(meta, shot)
    assert seen[0]["count"] == 24
    assert seen[0]["views"] == ("DTL",)
    ticks = [moment.capture_tick for moment in plan.moments]
    assert ticks[0] == 0
    assert ticks[-1] == 29
    assert len(ticks) == 24
    source = Path(anchor.__file__).read_text(encoding="utf-8")
    assert "select_dtl_large" in source
    assert "15, 16, 17, 18" not in source


def test_preserved_selection_reproduces_the_dtl_large_ticks() -> None:
    shot = dv.SHOTS / anchor.PRESERVED_SHOT_ID
    if not (shot / "metadata.json").is_file():
        pytest.skip("preserved fresh_v2 shot is not on this machine")
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = base.select_dtl_large(meta, shot)
    ticks = [moment.capture_tick for moment in plan.moments]
    assert ticks == _EXPECTED_TICKS
    loaded = dv.load_frames(shot, plan.moments)
    assert len(loaded) == 24
    assert {item.frame.view for item in loaded} == {"DTL"}


def test_request_keeps_the_simplified_catalog_and_omits_prior_output(
    tmp_path: Path,
) -> None:
    shot = _write_shot(tmp_path / anchor.PRESERVED_SHOT_ID, 24)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    moments = base.select_dtl_large(meta, shot).moments
    loaded = dv.load_frames(shot, moments)
    plain = base.build_request(moments, loaded)
    request = anchor.build_request(moments, loaded)
    assert request.system == base.SYSTEM == plain.system
    assert request.instructions.startswith(base.INSTRUCTIONS)
    assert request.schema == plain.schema
    assert request.schema_name == base.SCHEMA_NAME
    assert request.reasoning_effort == dv.REASONING_EFFORT
    assert [image.data for image in request.images] == [
        image.data for image in plain.images
    ]
    assert len(request.images) == 24
    assert all(image.detail == dv.DETAIL for image in request.images)
    assert all(image.label.startswith("DTL ") for image in request.images)
    projected = base.simplified_catalog()
    assert [row["id"] for row in projected] == [fault.id for fault in catalog.CATALOG]
    assert len(projected) == 26
    assert set(projected[0]) == {"id", "name", "visual_definition", "preferred_views"}
    prompt = request.system + request.instructions + request.evidence
    for field in (
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
    ):
        assert f'"{field}"' not in prompt
    assert anchor.CONTACT_ANCHOR_TEXT.strip() in request.instructions
    assert anchor.CONTACT_ANCHOR_TEXT.strip() in request.evidence
    assert _P_LABEL.search(anchor.CONTACT_ANCHOR_TEXT) is None
    assert "canonical" not in anchor.CONTACT_ANCHOR_TEXT.lower()
    addition = request.instructions[len(base.INSTRUCTIONS) :]
    for fault_id in (
        "HAND_PATH_OUT",
        "STEEP_DELIVERY",
        "OUT_TO_IN_DELIVERY",
        "EARLY_RELEASE",
    ):
        assert fault_id not in addition
    saved = base.RESULTS / anchor.PRESERVED_SHOT_ID / "reply.json"
    if saved.is_file():
        previous = json.loads(saved.read_text(encoding="utf-8"))
        for item in previous["payload"]["temporal_orientation"]:
            basis = item["visual_basis"]
            assert basis not in prompt


def test_anchor_rejects_after_strike_before_contact_and_keeps_the_payload() -> None:
    ticks = {15, 31, 42}
    payload = _no_fault(
        [15, 31, 42],
        {15: "after_strike", 31: "after_strike", 42: "after_strike"},
    )
    original = json.dumps(payload)
    problems = anchor.validate_reply(payload, ticks)
    assert json.dumps(payload) == original
    assert any(
        "capture_tick 15" in problem and "after_strike is invalid" in problem
        for problem in problems
    )
    assert any(
        "capture_tick 31" in problem and "after_strike is invalid" in problem
        for problem in problems
    )
    assert not any("capture_tick 42" in problem for problem in problems)


def test_anchor_rejects_a_pre_contact_state_after_contact() -> None:
    ticks = {15, 40, 42, 51}
    payload = _no_fault(
        [15, 40, 42, 51],
        {
            15: "setup",
            40: "moving_toward_ball",
            42: "moving_toward_ball",
            51: "after_strike",
        },
    )
    problems = anchor.validate_reply(payload, ticks)
    assert any(
        "capture_tick 42" in problem and "must be after_strike" in problem
        for problem in problems
    )
    assert not any("capture_tick 40" in problem for problem in problems)
    legal = _no_fault(
        [15, 40, 42, 51],
        {
            15: "setup",
            40: "uncertain",
            42: "after_strike",
            51: "after_strike",
        },
    )
    assert anchor.validate_reply(legal, ticks) == []


def test_anchor_does_not_accept_or_reject_fault_candidates() -> None:
    ticks = {15, 31, 42}
    kept = _classified(
        31, {15: "moving_away", 31: "moving_toward_ball", 42: "after_strike"}
    )
    assert anchor.validate_reply(kept, ticks) == []
    rejected = _classified(
        31, {15: "after_strike", 31: "after_strike", 42: "after_strike"}
    )
    problems = anchor.validate_reply(rejected, ticks)
    assert problems
    assert not any("HAND_PATH_OUT" in problem for problem in problems)
    assert not any("fault_candidates" in problem for problem in problems)
    post_contact = _classified(
        42, {15: "setup", 31: "moving_toward_ball", 42: "after_strike"}
    )
    assert anchor.validate_reply(post_contact, ticks) == []


def test_dry_run_makes_no_openai_request_and_skips_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / anchor.PRESERVED_SHOT_ID, 8)
    calls: list[object] = []

    def boom(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("OpenAI was called")

    monkeypatch.setattr(dv, "send_openai", boom)
    monkeypatch.setattr(anchor, "RESULTS", tmp_path / "contact_anchor")
    report = anchor.execute(shot.name, shots_root=shot.parent)
    assert calls == []
    assert report["sent"] is False
    assert report["comparison"] == "deferred_until_send"
    out = Path(report["result_directory"])
    assert (out / "request.json").is_file()
    assert (out / "review_sheet.png").is_file()
    assert not (out / "reply.json").exists()
    assert not (out / "comparison.json").exists()
    stored = json.loads((out / "request.json").read_text(encoding="utf-8"))
    assert stored["contact_anchor_text"] == anchor.CONTACT_ANCHOR_TEXT
    assert stored["system"] == base.SYSTEM


def test_existing_results_and_sources_cannot_be_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / anchor.PRESERVED_SHOT_ID, 8)
    simple_root = tmp_path / "simple_visual_classifier"
    catalog_root = tmp_path / "catalog_v1"
    matrix_root = tmp_path / "camera_budget_matrix"
    for folder in (simple_root, catalog_root, matrix_root):
        folder.mkdir()
        (folder / "report.json").write_text('{"keep": true}\n', encoding="utf-8")
    sources = [
        Path(base.__file__),
        Path(catalog.__file__),
        Path(dv.__file__),
        _repo() / "src" / "caddiej" / "video" / "jpeg_size.py",
    ]
    before = {path: path.read_bytes() for path in sources}
    saved = base.RESULTS / anchor.PRESERVED_SHOT_ID / "reply.json"
    saved_bytes = saved.read_bytes() if saved.is_file() else None
    original_simple_results = base.RESULTS
    monkeypatch.setattr(base, "RESULTS", simple_root)
    monkeypatch.setattr(dv, "RESULTS", catalog_root)
    monkeypatch.setattr(matrix, "RESULTS", matrix_root)
    with pytest.raises(RuntimeError):
        anchor.execute(shot.name, shots_root=shot.parent, results_root=simple_root)
    with pytest.raises(RuntimeError):
        anchor.execute(shot.name, shots_root=shot.parent, results_root=catalog_root)
    with pytest.raises(RuntimeError):
        anchor.execute(shot.name, shots_root=shot.parent, results_root=matrix_root)
    with pytest.raises(RuntimeError):
        anchor.execute(
            shot.name,
            shots_root=shot.parent,
            results_root=_repo() / "src",
        )
    with pytest.raises(RuntimeError):
        anchor.execute(
            shot.name,
            shots_root=shot.parent,
            results_root=_repo() / "research" / "temporal_evidence",
        )
    report = anchor.execute(
        shot.name,
        shots_root=shot.parent,
        results_root=tmp_path / "simple_visual_classifier_contact_anchor",
    )
    assert Path(report["result_directory"]).parent.name == (
        "simple_visual_classifier_contact_anchor"
    )
    for folder in (simple_root, catalog_root, matrix_root):
        assert (folder / "report.json").read_text(
            encoding="utf-8"
        ) == '{"keep": true}\n'
    for path, content in before.items():
        assert path.read_bytes() == content
    if saved_bytes is not None:
        assert saved.read_bytes() == saved_bytes
    assert anchor.RESULTS != original_simple_results
    assert anchor.RESULTS.name == "simple_visual_classifier_contact_anchor"


def test_comparison_describes_faults_without_choosing_a_winner() -> None:
    old = _reply(
        [15, 31, 42],
        {15: "setup", 31: "after_strike", 42: "after_strike"},
        [("HAND_PATH_OUT", 0.88, [31, 33]), ("EARLY_RELEASE", 0.5, [29])],
    )
    new = _reply(
        [15, 31, 42],
        {15: "setup", 31: "moving_toward_ball", 42: "after_strike"},
        [("HAND_PATH_OUT", 0.7, [35, 37]), ("STEEP_DELIVERY", 0.8, [35])],
    )
    comparison = anchor.build_comparison(old, new)
    assert comparison["false_early_after_strike_boundary_disappeared"] is True
    assert comparison["old_after_strike_before_contact"] == [31]
    assert comparison["new_after_strike_before_contact"] == []
    assert comparison["faults_removed"] == ["EARLY_RELEASE"]
    assert comparison["faults_added"] == ["STEEP_DELIVERY"]
    remained = comparison["faults_remaining"]
    assert remained[0]["fault_id"] == "HAND_PATH_OUT"
    assert remained[0]["evidence_ticks_moved"] is True
    assert "winner" not in comparison
    assert "ground truth" in comparison["note"]


def _repo() -> Path:
    return Path(anchor.__file__).resolve().parents[2]


def _classified(evidence_tick: int, states: dict[int, str]) -> dict:
    return {
        "status": "classified",
        "handedness": _hand(),
        "temporal_orientation": _orient(sorted(states), states),
        "fault_candidates": [
            {
                "fault_id": "HAND_PATH_OUT",
                "confidence": 0.88,
                "evidence": [
                    {
                        "capture_tick": evidence_tick,
                        "visual_basis": "The hands move out toward the ball.",
                    }
                ],
            }
        ],
    }


def _no_fault(ticks: list[int], states: dict[int, str]) -> dict:
    return {
        "status": "no_proven_fault",
        "handedness": _hand(),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [],
    }


def _reply(
    ticks: list[int],
    states: dict[int, str],
    faults: list[tuple[str, float, list[int]]],
) -> dict:
    return {
        "model": "gpt-5-mini",
        "latency_seconds": 1.5,
        "usage": {"input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 0},
        "payload": {
            "status": "classified",
            "handedness": _hand(),
            "temporal_orientation": _orient(ticks, states),
            "fault_candidates": [
                {
                    "fault_id": fault_id,
                    "confidence": confidence,
                    "evidence": [
                        {"capture_tick": tick, "visual_basis": "Visible in this frame."}
                        for tick in evidence
                    ],
                }
                for fault_id, confidence, evidence in faults
            ],
        },
    }


def _hand() -> dict:
    return {
        "side": "right",
        "confidence": 0.9,
        "visual_basis": "The address frame shows which side is closer to the target.",
    }


def _orient(ticks: list[int], states: dict[int, str]) -> list[dict]:
    return [
        {
            "capture_tick": tick,
            "state": states[tick],
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
