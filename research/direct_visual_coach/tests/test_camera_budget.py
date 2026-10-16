"""Camera mode and image budget for the Catalog V1 matrix. No OpenAI calls."""

from __future__ import annotations

import json
from pathlib import Path

import direct_visual_test as dv
import experiment_runner as experiment
import fault_catalog as catalog
import pytest
from PIL import Image


def test_matrix_has_nine_configurations() -> None:
    names = [name for name, _mode, _budget in experiment.MATRIX]
    assert names == [
        "DTL_SMALL",
        "DTL_MEDIUM",
        "DTL_LARGE",
        "FO_SMALL",
        "FO_MEDIUM",
        "FO_LARGE",
        "BOTH_SMALL",
        "BOTH_MEDIUM",
        "BOTH_LARGE",
    ]
    assert len(names) == len(set(names)) == 9


def test_image_budgets_are_eight_sixteen_and_twenty_four() -> None:
    assert experiment.IMAGE_BUDGETS == {"SMALL": 8, "MEDIUM": 16, "LARGE": 24}
    for _name, mode, budget in experiment.MATRIX:
        moments = experiment.moment_budget(mode, budget)
        views = experiment.views_for(mode)
        assert moments * len(views) == experiment.IMAGE_BUDGETS[budget]


def test_both_splits_the_budget_across_the_two_views() -> None:
    assert experiment.moment_budget("BOTH", "SMALL") == 4
    assert experiment.moment_budget("BOTH", "MEDIUM") == 8
    assert experiment.moment_budget("BOTH", "LARGE") == 12
    assert experiment.moment_budget("DTL", "LARGE") == 24
    assert experiment.moment_budget("FO", "MEDIUM") == 16


def test_budgets_keep_first_and_last_and_scale_the_gap() -> None:
    times = [(index, index * 0.05) for index in range(48)]
    meta = _meta(*_pair(times))
    motion = {index: float(index % 5) for index in range(48)}
    for count in (4, 8, 12, 16, 24):
        first = dv.select_moments(meta, count=count, motion=motion)
        second = dv.select_moments(meta, count=count, motion=motion)
        ticks = [moment.capture_tick for moment in first]
        assert ticks == [moment.capture_tick for moment in second]
        assert ticks[0] == 0 and ticks[-1] == 47
        assert len(ticks) == count
        assert ticks == sorted(ticks)
        norms = [tick / 47 for tick in ticks]
        gaps = [right - left for left, right in zip(norms, norms[1:])]
        assert max(gaps) <= dv.max_gap_fraction(count) + 1e-9


def test_motion_still_changes_a_larger_budget() -> None:
    times = [(index, index * 0.05) for index in range(48)]
    meta = _meta(*_pair(times))
    flat = {index: 1.0 for index in range(48)}
    peaked = dict(flat)
    peaked[30] = 100.0
    peaked[31] = 90.0
    flat_ticks = _ticks(meta, count=16, motion=flat)
    peak_ticks = _ticks(meta, count=16, motion=peaked)
    assert flat_ticks[0] == peak_ticks[0] == 0
    assert flat_ticks[-1] == peak_ticks[-1] == 47
    assert flat_ticks != peak_ticks


def test_single_camera_motion_uses_that_camera(tmp_path: Path) -> None:
    shot = _write_motion_shot(tmp_path / "shot")
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    dtl = _ticks(meta, count=8, shot=shot, views=("DTL",))
    fo = _ticks(meta, count=8, shot=shot, views=("FO",))
    assert dtl[0] == fo[0] == 0
    assert dtl[-1] == fo[-1] == 23
    assert dtl != fo
    dtl_late = len([tick for tick in dtl if tick >= 12])
    fo_late = len([tick for tick in fo if tick >= 12])
    assert dtl_late > fo_late
    assert (len(fo) - fo_late) > (len(dtl) - dtl_late)


def test_views_are_limited_to_the_requested_camera() -> None:
    times = [(index, float(index)) for index in range(20)]
    meta = _meta(*_pair(times))
    motion = {index: 1.0 for index in range(20)}
    dtl = dv.select_moments(meta, count=8, motion=motion, views=("DTL",))
    fo = dv.select_moments(meta, count=8, motion=motion, views=("FO",))
    both = dv.select_moments(meta, count=4, motion=motion, views=("DTL", "FO"))
    assert all(frame.view == "DTL" for moment in dtl for frame in moment.frames)
    assert all(frame.view == "FO" for moment in fo for frame in moment.frames)
    assert all(len(moment.frames) == 1 for moment in (*dtl, *fo))
    for moment in both:
        assert [frame.view for frame in moment.frames] == ["DTL", "FO"]
        assert moment.frames[0].capture_tick == moment.frames[1].capture_tick


def test_short_source_uses_every_valid_moment() -> None:
    times = [(index, float(index)) for index in range(6)]
    meta = _meta(*_pair(times))
    motion = {index: 1.0 for index in range(6)}
    moments = dv.select_moments(meta, count=24, motion=motion, views=("DTL",))
    assert [moment.capture_tick for moment in moments] == [0, 1, 2, 3, 4, 5]


def test_single_camera_prompt_does_not_claim_the_other_view(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shot", 12)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    motion = {index: 1.0 for index in range(12)}
    dtl_moments = dv.select_moments(meta, count=8, motion=motion, views=("DTL",))
    fo_moments = dv.select_moments(meta, count=8, motion=motion, views=("FO",))
    both_moments = dv.select_moments(meta, count=8, motion=motion)
    dtl = dv.build_request(dtl_moments, dv.load_frames(shot, dtl_moments))
    fo = dv.build_request(fo_moments, dv.load_frames(shot, fo_moments))
    both = dv.build_request(both_moments, dv.load_frames(shot, both_moments))
    dtl_preamble = dtl.evidence.split("VISUAL FAULT CATALOG", 1)[0]
    fo_preamble = fo.evidence.split("VISUAL FAULT CATALOG", 1)[0]
    assert "No Face-On frame is attached." in dtl_preamble
    assert "No Down-the-Line frame is attached." in fo_preamble
    assert "Down-the-Line and Face-On images" not in dtl.system
    assert "Down-the-Line and Face-On images" not in fo.system
    assert "The two cameras were requested together." not in dtl.instructions
    assert "The two cameras were requested together." not in fo.instructions
    assert "DTL and FO with the same capture_tick are one moment." not in dtl_preamble
    assert both.system == dv.SYSTEM
    assert both.instructions == dv.INSTRUCTIONS
    assert "DTL and FO with the same capture_tick are one moment." in both.evidence
    assert len(dtl.images) == 8
    assert all(image.label.startswith("DTL ") for image in dtl.images)
    assert all(image.label.startswith("FO ") for image in fo.images)


def test_unavailable_preferred_view_rejects_only_that_fault(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shot", 12)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    motion = {index: 1.0 for index in range(12)}
    moments = dv.select_moments(meta, count=8, motion=motion, views=("DTL",))
    request = dv.build_request(moments, dv.load_frames(shot, moments))
    assert request.images
    sent = dv.sent_images(moments)
    assert {view for view, _tick in sent} == {"DTL"}
    face = catalog.FAULTS_BY_ID["BACKSWING_LEAD_ARM_COLLAPSED"]
    depth = catalog.FAULTS_BY_ID["EARLY_EXTENSION"]
    assert face.preferred_views == ("FO",)
    assert depth.preferred_views == ("DTL",)
    rejected = _classified(
        "BACKSWING_LEAD_ARM_COLLAPSED",
        [_see(moments[3].capture_tick, "DTL", "The lead arm is folded.")],
        [moment.capture_tick for moment in moments],
        _states(moments),
    )
    problems = dv.validate(rejected, sent)
    assert any("was not supplied" in problem for problem in problems)
    early, late = moments[0].capture_tick, moments[4].capture_tick
    accepted = _classified(
        "EARLY_EXTENSION",
        [
            _see(early, "DTL", "Address shows the pelvis depth."),
            _see(late, "DTL", "The pelvis has moved closer to the ball."),
        ],
        [moment.capture_tick for moment in moments],
        _states(moments, setup=early, delivery=late),
    )
    assert dv.validate(accepted, sent) == []
    assert (
        dv.validate(_no_fault([moment.capture_tick for moment in moments]), sent) == []
    )


def test_runs_are_independent_of_order(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-a", 16)
    first = experiment.run_configuration(
        "shot-a",
        "DTL",
        "SMALL",
        shots_root=shot.parent,
        out_dir=tmp_path / "first",
    )
    experiment.run_configuration(
        "shot-a",
        "FO",
        "SMALL",
        shots_root=shot.parent,
        out_dir=tmp_path / "fo",
    )
    second = experiment.run_configuration(
        "shot-a",
        "DTL",
        "SMALL",
        shots_root=shot.parent,
        out_dir=tmp_path / "second",
    )
    assert first["selected_ticks"] == second["selected_ticks"]
    assert _evidence(tmp_path / "first") == _evidence(tmp_path / "second")
    assert "No Face-On frame is attached." in _evidence(tmp_path / "first")
    assert "No Face-On frame is attached." not in _evidence(tmp_path / "fo")


def test_matrix_matches_image_budgets_and_leaves_catalog_results(
    tmp_path: Path,
) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-matrix", 36)
    catalog_dir = tmp_path / "catalog_v1" / shot.name
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "report.json").write_text('{"keep": true}\n', encoding="utf-8")

    def explode(_request: object) -> object:
        raise AssertionError("OpenAI was called without --send")

    comparison = experiment.run_matrix(
        shot.name,
        send=False,
        shots_root=shot.parent,
        results_root=tmp_path / "camera_budget_matrix",
        run_id="run-a",
        complete=explode,
    )
    assert (catalog_dir / "report.json").read_text(
        encoding="utf-8"
    ) == '{"keep": true}\n'
    assert experiment.RESULTS != dv.RESULTS
    root = tmp_path / "camera_budget_matrix" / shot.name / "run-a"
    assert (root / "comparison.json").is_file()
    assert (root / "comparison.md").is_file()
    expected = {
        "DTL_SMALL": (8, 8),
        "DTL_MEDIUM": (16, 16),
        "DTL_LARGE": (24, 24),
        "FO_SMALL": (8, 8),
        "FO_MEDIUM": (16, 16),
        "FO_LARGE": (24, 24),
        "BOTH_SMALL": (4, 8),
        "BOTH_MEDIUM": (8, 16),
        "BOTH_LARGE": (12, 24),
    }
    by_name = {row["configuration"]: row for row in comparison["rows"]}
    assert set(by_name) == set(expected)
    for name, (moments, images) in expected.items():
        row = by_name[name]
        folder = root / name
        assert row["temporal_moments"] == moments
        assert row["image_count"] == images
        assert row["sent"] is False
        assert row["status"] is None
        assert not (folder / "reply.json").exists()
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        views = {image["view"] for image in request["images"]}
        if name.startswith("DTL"):
            assert views == {"DTL"}
        elif name.startswith("FO"):
            assert views == {"FO"}
        else:
            assert views == {"DTL", "FO"}
            ticks = [image["capture_tick"] for image in request["images"]]
            assert ticks[0::2] == ticks[1::2]
        assert (folder / "review_sheet.png").is_file()
        assert (folder / "selected_frames.json").is_file()
    assert comparison["no_proven_fault"] == []
    assert comparison["proven_faults"] == []
    assert comparison["fastest"] is None


def test_comparison_accepts_no_proven_fault_and_candidates() -> None:
    empty = [
        _report(name, mode, budget, status="no_proven_fault", candidates=[])
        for name, mode, budget in experiment.MATRIX
    ]
    none = experiment.build_comparison("shot", "run", empty)
    assert none["no_proven_fault"] == [
        name for name, _mode, _budget in experiment.MATRIX
    ]
    assert none["proven_faults"] == []
    assert none["agreement"] == []

    mixed = [
        _report(
            "DTL_MEDIUM",
            "DTL",
            "MEDIUM",
            status="classified",
            candidates=["HAND_PATH_OUT"],
            confidence=0.8,
        ),
        _report(
            "DTL_LARGE",
            "DTL",
            "LARGE",
            status="classified",
            candidates=["HAND_PATH_OUT", "EARLY_EXTENSION"],
            confidence=0.91,
        ),
        _report(
            "BOTH_MEDIUM",
            "BOTH",
            "MEDIUM",
            status="classified",
            candidates=["HAND_PATH_OUT"],
            confidence=0.7,
        ),
        _report(
            "BOTH_LARGE",
            "BOTH",
            "LARGE",
            status="classified",
            candidates=["HAND_PATH_OUT"],
            confidence=0.72,
        ),
        _report("FO_SMALL", "FO", "SMALL", status="no_proven_fault", candidates=[]),
    ]
    result = experiment.build_comparison("shot", "run", mixed)
    by_id = {item["fault_id"]: item for item in result["agreement"]}
    assert by_id["HAND_PATH_OUT"]["configurations"] == 4
    assert by_id["HAND_PATH_OUT"]["of"] == 5
    assert by_id["HAND_PATH_OUT"]["where"] == [
        "DTL_MEDIUM",
        "DTL_LARGE",
        "BOTH_MEDIUM",
        "BOTH_LARGE",
    ]
    assert by_id["EARLY_EXTENSION"]["configurations"] == 1
    assert result["faults_repeated"][0]["fault_id"] == "HAND_PATH_OUT"
    assert result["faults_seen_once"][0]["fault_id"] == "EARLY_EXTENSION"
    assert result["no_proven_fault"] == ["FO_SMALL"]
    assert set(result["proven_faults"]) == {
        "DTL_MEDIUM",
        "DTL_LARGE",
        "BOTH_MEDIUM",
        "BOTH_LARGE",
    }
    assert result["lowest_input_tokens"]["configuration"] == "FO_SMALL"
    assert "HAND_PATH_OUT: 4/5" in experiment._comparison_md(result)
    assert "DTL:" in experiment._comparison_md(result)


def test_cli_does_not_send_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-cli", 12)
    monkeypatch.setattr(experiment, "RESULTS", tmp_path / "camera_budget_matrix")
    monkeypatch.setattr(dv, "SHOTS", shot.parent)

    async def explode(_request: object) -> object:
        raise AssertionError("OpenAI was called without --send")

    monkeypatch.setattr(dv, "send_openai", explode)
    experiment.main([shot.name])
    written = list((tmp_path / "camera_budget_matrix" / shot.name).rglob("reply.json"))
    assert written == []
    assert list(
        (tmp_path / "camera_budget_matrix" / shot.name).rglob("comparison.json")
    )


def _ticks(meta: dict, **kwargs) -> list[int]:
    return [moment.capture_tick for moment in dv.select_moments(meta, **kwargs)]


def _evidence(folder: Path) -> str:
    payload = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    return payload["system"] + payload["instructions"] + payload["evidence"]


def _report(
    name: str,
    mode: str,
    budget: str,
    *,
    status: str,
    candidates: list[str],
    confidence: float | None = None,
) -> dict:
    tokens = {"SMALL": 100, "MEDIUM": 200, "LARGE": 300}[budget]
    return {
        "configuration": name,
        "camera_mode": mode,
        "image_budget": budget,
        "temporal_moments": experiment.moment_budget(mode, budget),
        "image_count": experiment.IMAGE_BUDGETS[budget],
        "selected_ticks": [1, 2],
        "status": status,
        "handedness": "right",
        "orientation_valid": True,
        "fault_candidates": candidates,
        "primary_fault": candidates[0] if candidates else None,
        "primary_confidence": confidence,
        "estimated_input_tokens": tokens,
        "input_tokens": tokens,
        "output_tokens": 10,
        "openai_latency_s": tokens / 100,
        "estimated_cost_usd": tokens / 10000,
        "sent": True,
    }


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


def _pair(ticks: list[tuple[int, float]]) -> tuple[list[dict], list[dict]]:
    behind = [_frame("swing_behind", tick, replay) for tick, replay in ticks]
    side = [_frame("swing_side", tick, replay) for tick, replay in ticks]
    return behind, side


def _meta(behind: list[dict], side: list[dict]) -> dict:
    return {
        "diagnostic_timeline": {
            "cameras": [
                {"camera_id": "swing_behind", "frames": behind},
                {"camera_id": "swing_side", "frames": side},
            ]
        }
    }


def _write_shot(folder: Path, count: int) -> Path:
    times = [(index, index * 0.1) for index in range(count)]
    behind, side = _pair(times)
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps(_meta(behind, side)), encoding="utf-8"
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


def _write_motion_shot(folder: Path) -> Path:
    """DTL changes late. FO changes early. The two motion scores should disagree."""

    count = 24
    times = [(index, index * 0.1) for index in range(count)]
    behind, side = _pair(times)
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps(_meta(behind, side)), encoding="utf-8"
    )
    for index in range(count):
        dtl_color = 0 if index < 12 else (0 if index % 2 == 0 else 255)
        fo_color = (0 if index % 2 == 0 else 255) if index < 12 else 0
        for camera, color, width in (
            ("swing_behind", dtl_color, 40),
            ("swing_side", fo_color, 48),
        ):
            directory = folder / "diagnostic" / camera
            directory.mkdir(parents=True, exist_ok=True)
            Image.new("L", (width, 32), color=color).save(
                directory / f"frame_{index:03d}.jpg", format="JPEG"
            )
    return folder


def _hand() -> dict:
    return {
        "side": "right",
        "confidence": 0.9,
        "visual_basis": "At address the lead side is closer to the target.",
    }


def _states(
    moments: list[dv.Moment], setup: int | None = None, delivery: int | None = None
) -> dict[int, str]:
    ticks = [moment.capture_tick for moment in moments]
    states = {tick: "moving_toward_ball" for tick in ticks}
    if setup is not None:
        states[setup] = "setup"
    if delivery is not None:
        states[delivery] = "moving_toward_ball"
    else:
        states[ticks[0]] = "setup"
    return states


def _orient(ticks: list[int], states: dict[int, str]) -> list[dict]:
    return [
        {
            "capture_tick": tick,
            "state": states[tick],
            "confidence": 0.8,
            "visual_basis": "The hands and club show this direction.",
        }
        for tick in ticks
    ]


def _see(tick: int, view: str, observation: str) -> dict:
    return {"capture_tick": tick, "view": view, "observation": observation}


def _classified(
    fault_id: str, evidence: list[dict], ticks: list[int], states: dict[int, str]
) -> dict:
    fault = catalog.FAULTS_BY_ID[fault_id]
    return {
        "status": "classified",
        "handedness": _hand(),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [
            {
                "fault_id": fault_id,
                "confidence": 0.8,
                "evidence": evidence,
                "why_required_evidence_is_met": ["Visible in the frames."]
                * len(fault.required_evidence),
                "alternatives_considered": [],
            }
        ],
    }


def _no_fault(ticks: list[int]) -> dict:
    states = {tick: "moving_toward_ball" for tick in ticks}
    states[ticks[0]] = "setup"
    return {
        "status": "no_proven_fault",
        "handedness": _hand(),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [],
    }
