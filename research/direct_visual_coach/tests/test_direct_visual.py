"""Frame selection and reply validation for the direct visual coach probe.

No test sends a request. A fake completer stands in for OpenAI when --send is exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from caddiej.coaching.templates import REINFORCEMENT, SPEECH

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import direct_visual_test as dv  # noqa: E402
import fault_catalog as catalog  # noqa: E402


def _frame(
    camera: str,
    tick: int,
    replay: float,
    *,
    sha: str | None = None,
    nbytes: int = 60_000,
    file: str | None = None,
) -> dict:
    return {
        "capture_index": tick,
        "offset_from_replay_start_seconds": replay,
        "offset_seconds": round(2.0 + replay, 4),
        "bytes": nbytes,
        "sha256": sha if sha is not None else f"{camera}-{tick}",
        "file": file
        if file is not None
        else f"diagnostic/{camera}/frame_{tick:03d}.jpg",
        "width": 1002 if camera == "swing_behind" else 1272,
        "height": 1480 if camera == "swing_behind" else 1490,
        "selected_phase": "P7" if tick == 15 else None,
    }


def _meta(behind: list[dict], side: list[dict]) -> dict:
    return {
        "diagnostic_timeline": {
            "cameras": [
                {"camera_id": "swing_behind", "frames": behind},
                {"camera_id": "swing_side", "frames": side},
            ]
        },
        "coach_metrics": {"file": "metrics/all_values.jpg"},
        "canonical": {"P1": 15},
        "reply": {"primary_issue": "ignore this model reply"},
    }


def _pair(ticks: list[tuple[int, float]], **kwargs) -> tuple[list[dict], list[dict]]:
    behind = [_frame("swing_behind", tick, replay, **kwargs) for tick, replay in ticks]
    side = [_frame("swing_side", tick, replay, **kwargs) for tick, replay in ticks]
    return behind, side


def _mean_gap(ticks: list[int]) -> float:
    gaps = [right - left for left, right in zip(ticks, ticks[1:])]
    assert gaps
    return sum(gaps) / len(gaps)


class _Reply:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.model = "gpt-5-mini-2025-08-07"
        self.latency_seconds = 1.25
        self.input_tokens = 1000
        self.output_tokens = 200
        self.reasoning_tokens = 10


def _request_ticks(request: object) -> list[int]:
    ticks: list[int] = []
    for image in request.images:  # type: ignore[attr-defined]
        tick = int(image.label.split(",")[0].rsplit(" ", 1)[-1])
        if tick not in ticks:
            ticks.append(tick)
    return ticks


def _reply_for(request: object) -> dict:
    return _no_fault(_request_ticks(request))


def _orient(ticks: list[int], states: dict[int, str] | None = None) -> list[dict]:
    rows = []
    for tick in sorted(set(ticks)):
        rows.append(
            {
                "capture_tick": tick,
                "state": "moving_toward_ball" if states is None else states[tick],
                "confidence": 0.8,
                "visual_basis": "The hands and club show this direction.",
            }
        )
    return rows


def _forward_states(ticks: list[int]) -> dict[int, str]:
    order = (
        "setup",
        "moving_away",
        "moving_away",
        "direction_change_region",
        "moving_toward_ball",
        "moving_toward_ball",
        "moving_toward_ball",
        "after_strike",
    )
    states = {}
    for index, tick in enumerate(sorted(set(ticks))):
        states[tick] = order[index] if index < len(order) else "after_strike"
    return states


def _hand(side: str = "right") -> dict:
    return {
        "side": side,
        "confidence": 0.9,
        "visual_basis": "At address the lead side is closer to the target.",
    }


def _no_fault(
    ticks: list[int],
    states: dict[int, str] | None = None,
    handedness: str = "right",
) -> dict:
    return {
        "status": "no_proven_fault",
        "handedness": _hand(handedness),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [],
    }


def _candidate(
    fault_id: str,
    evidence: list[dict],
    *,
    confidence: float = 0.8,
    alternatives: list[dict] | None = None,
) -> dict:
    fault = catalog.FAULTS_BY_ID[fault_id]
    return {
        "fault_id": fault_id,
        "confidence": confidence,
        "evidence": evidence,
        "why_required_evidence_is_met": ["Visible in the frames."]
        * len(fault.required_evidence),
        "alternatives_considered": [] if alternatives is None else alternatives,
    }


def _classified(
    fault_id: str, evidence: list[dict], ticks: list[int], states: dict[int, str]
) -> dict:
    return {
        "status": "classified",
        "handedness": _hand(),
        "temporal_orientation": _orient(ticks, states),
        "fault_candidates": [_candidate(fault_id, evidence)],
    }


def _see(tick: int, view: str, observation: str) -> dict:
    return {"capture_tick": tick, "view": view, "observation": observation}


def _ticks(meta: dict, **kwargs) -> list[int]:
    return [moment.capture_tick for moment in dv.select_moments(meta, **kwargs)]


def test_fast_motion_is_sampled_more_densely_than_the_quiet_swing() -> None:
    times = [(index, index * 0.1) for index in range(24)]
    meta = _meta(*_pair(times))
    bare = _meta(*_pair(times))
    bare.pop("canonical")
    bare.pop("reply")
    for camera in bare["diagnostic_timeline"]["cameras"]:
        for frame in camera["frames"]:
            frame.pop("selected_phase")
    motion = {
        index: (0.0 if index == 0 else 1.0 if index < 12 else 40.0)
        for index in range(24)
    }
    ticks = _ticks(meta, motion=motion)
    assert ticks == _ticks(bare, motion=motion)
    assert ticks[0] == 0 and ticks[-1] == 23
    assert len(ticks) == dv.MOMENT_COUNT == 8
    assert ticks == sorted(ticks)
    quiet = [tick for tick in ticks if tick < 12]
    fast = [tick for tick in ticks if tick >= 12]
    assert len(fast) > len(quiet)
    assert len(quiet) >= 2
    assert _mean_gap(fast) < _mean_gap(quiet)
    moments = dv.select_moments(meta, motion=motion)
    assert all(
        moment.frames[0].view == "DTL" and moment.frames[1].view == "FO"
        for moment in moments
    )


def test_high_motion_keeps_a_neighboring_moment() -> None:
    count = 36
    times = [(index, index * 0.05) for index in range(count)]
    motion = {index: 3.0 for index in range(count)}
    motion[0] = 0.0
    motion[18] = 80.0
    motion[17] = 25.0
    ticks = _ticks(_meta(*_pair(times)), motion=motion)
    assert ticks[0] == 0 and ticks[-1] == count - 1
    assert ticks == _ticks(_meta(*_pair(times)), motion=motion)
    assert 17 in ticks and 18 in ticks
    assert any(right - left == 1 for left, right in zip(ticks, ticks[1:]))


def test_selection_covers_normalized_replay_bands() -> None:
    count = 25
    times = [(index, float(index)) for index in range(count)]
    motion = {index: float((index * 3) % 7) for index in range(count)}
    meta = _meta(*_pair(times))
    bare = _meta(*_pair(times))
    bare.pop("canonical")
    bare.pop("reply")
    ticks = _ticks(meta, motion=motion)
    assert ticks == _ticks(bare, motion=motion)
    assert ticks == _ticks(meta, motion=motion)
    norms = [tick / (count - 1) for tick in ticks]
    for band in range(dv.COVERAGE_BANDS):
        lo = band / dv.COVERAGE_BANDS
        hi = (band + 1) / dv.COVERAGE_BANDS
        assert any(
            (lo <= norm < hi) or (band == dv.COVERAGE_BANDS - 1 and norm == 1.0)
            for norm in norms
        )
    moments = dv.select_moments(meta, motion=motion)
    assert all(
        moment.frames[0].view == "DTL" and moment.frames[1].view == "FO"
        for moment in moments
    )


def test_selection_fills_a_wide_replay_gap_before_clustering() -> None:
    count = 40
    times = [(index, index * 0.05) for index in range(count)]
    motion = {index: 0.4 for index in range(count)}
    motion[0] = 0.0
    for index in range(4):
        motion[index] = 100.0
        motion[count - 1 - index] = 100.0
    ticks = _ticks(_meta(*_pair(times)), motion=motion)
    norms = [tick / (count - 1) for tick in ticks]
    gaps = [right - left for left, right in zip(norms, norms[1:])]
    assert max(gaps) <= dv.max_gap_fraction(dv.MOMENT_COUNT) + 1e-9
    assert any(0.4 <= norm <= 0.6 for norm in norms)


def test_local_pairing_cannot_starve_the_middle() -> None:
    count = 40
    times = [(index, index * 0.05) for index in range(count)]
    motion = {index: 1.0 for index in range(count)}
    motion[0] = 0.0
    for index in range(count - 4, count):
        motion[index] = 100.0
    ticks = _ticks(_meta(*_pair(times)), motion=motion)
    norms = [tick / (count - 1) for tick in ticks]
    assert any(0.4 <= norm <= 0.6 for norm in norms)
    assert len([norm for norm in norms if norm >= 0.75]) <= 3
    gaps = [right - left for left, right in zip(norms, norms[1:])]
    assert max(gaps) <= dv.max_gap_fraction(dv.MOMENT_COUNT) + 1e-9


def test_flat_motion_still_covers_early_middle_and_finish() -> None:
    times = [(index, index * 0.1) for index in range(24)]
    motion = {index: 0.0 if index == 0 else 5.0 for index in range(24)}
    ticks = _ticks(_meta(*_pair(times)), motion=motion)
    assert ticks[0] == 0 and ticks[-1] == 23
    assert any(1 <= tick <= 7 for tick in ticks)
    assert any(8 <= tick <= 15 for tick in ticks)
    assert any(16 <= tick <= 22 for tick in ticks)


def test_choosing_a_subset_requires_motion_or_images() -> None:
    times = [(index, index * 0.1) for index in range(20)]
    with pytest.raises(ValueError, match="motion"):
        dv.select_moments(_meta(*_pair(times)))


def test_selection_drops_prereplay_blanks_duplicates_and_one_camera() -> None:
    behind = [
        _frame("swing_behind", 1, -0.4),
        _frame("swing_behind", 2, 0.0),
        _frame("swing_behind", 3, 0.4),
        _frame("swing_behind", 4, 0.8, sha="same"),
        _frame("swing_behind", 5, 1.2, sha="same"),
        _frame("swing_behind", 6, 1.6, nbytes=100),
        _frame("swing_behind", 7, 2.0),
        _frame("swing_side", 8, 2.4, file="swing_behind/frame_008.jpg"),
    ]
    side = [
        _frame("swing_side", 1, -0.4),
        _frame("swing_side", 2, 0.0),
        _frame("swing_side", 4, 0.8, sha="side-4"),
        _frame("swing_side", 5, 1.2, sha="side-5"),
        _frame("swing_side", 6, 1.6),
        _frame("swing_side", 7, 2.0),
        _frame("swing_side", 8, 2.4),
    ]
    ticks = _ticks(_meta(behind, side))
    assert ticks[0] == 2
    assert ticks[-1] == 7
    assert 1 not in ticks
    assert 3 not in ticks
    assert 5 not in ticks
    assert 6 not in ticks
    assert 8 not in ticks


def test_selection_returns_every_moment_when_fewer_than_eight() -> None:
    meta = _meta(*_pair([(4, 0.0), (6, 1.0), (9, 2.0)]))
    assert _ticks(meta) == [4, 6, 9]


def test_selection_requires_a_diagnostic_timeline() -> None:
    with pytest.raises(ValueError, match="diagnostic timeline"):
        dv.select_moments({})


def _set_state(payload: dict, tick: int, state: str) -> None:
    for item in payload["temporal_orientation"]:
        if item["capture_tick"] == tick:
            item["state"] = state
            return
    raise AssertionError(f"tick {tick} is not in the orientation")


def _pair_sent(*ticks: int) -> set[tuple[str, int]]:
    return {(view, tick) for tick in ticks for view in ("DTL", "FO")}


def _setup_fault(tick: int = 15) -> dict:
    return _classified(
        "SETUP_WEIGHT_TOWARD_TOES",
        [
            _see(
                tick,
                "DTL",
                "At address the knees sit ahead of the ankles and the heels look unloaded.",
            )
        ],
        [tick],
        {tick: "setup"},
    )


def _shoulder_fault(early: int = 35, late: int = 41) -> dict:
    return _classified(
        "TRAIL_SHOULDER_OUT_EARLY",
        [
            _see(early, "DTL", "The trail shoulder moves outward toward the ball."),
            _see(late, "DTL", "The hands move outward with the trail shoulder."),
        ],
        [early, late],
        {early: "direction_change_region", late: "moving_toward_ball"},
    )


def test_orientation_accepts_every_sent_tick_once() -> None:
    assert dv.validate(_no_fault([15, 30]), _pair_sent(15, 30)) == []


def test_orientation_rejects_a_missing_tick() -> None:
    payload = _no_fault([15, 30])
    payload["temporal_orientation"] = [
        item for item in payload["temporal_orientation"] if item["capture_tick"] != 30
    ]
    problems = dv.validate(payload, _pair_sent(15, 30))
    assert any("missing sent ticks: 30" in problem for problem in problems)


def test_orientation_rejects_a_duplicate_tick() -> None:
    payload = _no_fault([15])
    payload["temporal_orientation"].append(dict(payload["temporal_orientation"][0]))
    problems = dv.validate(payload, _pair_sent(15))
    assert any("duplicated" in problem for problem in problems)


def test_orientation_rejects_an_illegal_state() -> None:
    payload = _no_fault([15])
    payload["temporal_orientation"][0]["state"] = "impact"
    problems = dv.validate(payload, _pair_sent(15))
    assert any("state is not allowed" in problem for problem in problems)


def test_orientation_rejects_a_chronological_regression() -> None:
    payload = _no_fault([15, 30], {15: "moving_toward_ball", 30: "moving_away"})
    problems = dv.validate(payload, _pair_sent(15, 30))
    assert any("regresses" in problem for problem in problems)


def test_orientation_allows_uncertain_between_forward_states() -> None:
    payload = _no_fault(
        [15, 20, 30],
        {15: "moving_away", 20: "uncertain", 30: "moving_toward_ball"},
    )
    assert dv.validate(payload, _pair_sent(15, 20, 30)) == []


def test_moving_away_cannot_evidence_a_downstream_cause() -> None:
    payload = _shoulder_fault()
    _set_state(payload, 35, "moving_away")
    problems = dv.validate(payload, _pair_sent(35, 41))
    assert any(
        "moving_away" in problem and "not allowed" in problem for problem in problems
    )


def test_after_strike_cannot_evidence_an_upstream_cause() -> None:
    payload = _shoulder_fault()
    _set_state(payload, 35, "after_strike")
    _set_state(payload, 41, "after_strike")
    problems = dv.validate(payload, _pair_sent(35, 41))
    assert any(
        "after_strike" in problem and "not allowed" in problem for problem in problems
    )


def test_moving_toward_ball_can_evidence_the_cause() -> None:
    assert dv.validate(_shoulder_fault(), _pair_sent(35, 41)) == []


def test_uncertain_orientation_can_be_insufficient_evidence() -> None:
    assert dv.validate(_no_fault([15], {15: "uncertain"}), _pair_sent(15)) == []


def test_uncertain_cause_is_not_coachable() -> None:
    payload = _setup_fault()
    _set_state(payload, 15, "uncertain")
    problems = dv.validate(payload, _pair_sent(15))
    assert any(
        "uncertain" in problem and "not allowed" in problem for problem in problems
    )


def test_zero_fault_result_is_accepted() -> None:
    assert dv.validate(_no_fault([15, 30]), _pair_sent(15, 30)) == []


def test_unknown_fault_is_rejected() -> None:
    payload = _setup_fault()
    payload["fault_candidates"][0]["fault_id"] = "UPPER_BODY_LATERAL_SLIDE"
    problems = dv.validate(payload, _pair_sent(15))
    assert any("not in the catalog" in problem for problem in problems)


def test_free_form_fault_cannot_enter() -> None:
    payload = _setup_fault()
    payload["fault_candidates"][0]["fault_id"] = "the trail shoulder moves out"
    payload["instruction"] = "Drop the trail shoulder."
    problems = dv.validate(payload, _pair_sent(15))
    assert any("not in the catalog" in problem for problem in problems)
    assert any("unexpected fields" in problem for problem in problems)


def test_unsent_evidence_tick_is_rejected() -> None:
    payload = _setup_fault(99)
    problems = dv.validate(payload, _pair_sent(15))
    assert any("was not sent" in problem for problem in problems)


def test_wrong_temporal_state_is_rejected() -> None:
    payload = _setup_fault()
    _set_state(payload, 15, "moving_toward_ball")
    problems = dv.validate(payload, _pair_sent(15))
    assert any("not allowed" in problem for problem in problems)


def test_required_evidence_must_be_addressed() -> None:
    payload = _setup_fault()
    payload["fault_candidates"][0]["why_required_evidence_is_met"] = ["Only one."]
    problems = dv.validate(payload, _pair_sent(15))
    assert any("every required item" in problem for problem in problems)


def test_baseline_frame_must_be_earlier() -> None:
    payload = _classified(
        "EARLY_EXTENSION",
        [_see(30, "DTL", "The pelvis is close to the ball.")],
        [15, 30],
        {15: "setup", 30: "moving_toward_ball"},
    )
    problems = dv.validate(payload, _pair_sent(15, 30))
    assert any("earlier baseline" in problem for problem in problems)


def test_valid_baseline_comparison_is_accepted() -> None:
    payload = _classified(
        "EARLY_EXTENSION",
        [
            _see(15, "DTL", "Address shows the pelvis depth."),
            _see(30, "DTL", "The pelvis has moved closer to the ball."),
        ],
        [15, 30],
        {15: "setup", 30: "moving_toward_ball"},
    )
    assert dv.validate(payload, _pair_sent(15, 30)) == []


def test_metrics_and_all_values_are_not_visual_proof() -> None:
    payload = _setup_fault()
    payload["fault_candidates"][0]["evidence"][0]["observation"] = (
        "The club path was 6 degrees."
    )
    assert any(
        "numerical" in problem for problem in dv.validate(payload, _pair_sent(15))
    )
    cited = _setup_fault()
    cited["fault_candidates"][0]["evidence"][0]["view"] = "ALL_VALUES"
    cited["fault_candidates"][0]["evidence"][0]["observation"] = (
        "The ALL_VALUES grid shows the path."
    )
    problems = dv.validate(cited, _pair_sent(15))
    assert any("DTL or FO" in problem for problem in problems)
    assert any("ALL_VALUES" in problem for problem in problems)


def test_model_coaching_fields_are_rejected() -> None:
    payload = _setup_fault()
    payload["instruction"] = (
        "Override the production lesson and say the lesson cue instead."
    )
    problems = dv.validate(payload, _pair_sent(15))
    assert any("unexpected fields" in problem for problem in problems)
    assert any("reconciliation" in problem for problem in problems)


def test_request_uses_originals_and_omits_the_production_lesson(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-a")
    calls: list[object] = []

    def complete(request: object) -> _Reply:
        calls.append(request)
        return _Reply(_no_fault([0]))

    report = dv.execute(
        "shot-a",
        send=False,
        shots_root=shot.parent,
        results_root=tmp_path / "results",
        complete=complete,
    )
    assert calls == []
    assert report["sent"] is False
    assert report["model"] == "gpt-5-mini"
    assert report["reasoning_effort"] == "minimal"
    assert report["production_reconciliation"] is False
    assert report["architecture"] == "visual_fault_catalog_v1"
    assert report["model_classification"] is None
    assert report["caddiej_coaching"] is None
    assert report["catalog_exposure"]["fields_withheld"] == [
        "coaching",
        "typical_consequences",
        "upstream_candidates",
    ]
    assert not (tmp_path / "results" / "shot-a" / "reply.json").exists()
    request = json.loads(
        (tmp_path / "results" / "shot-a" / "request.json").read_text(encoding="utf-8")
    )
    blob = request["system"] + request["instructions"] + request["evidence"]
    for lines in (*SPEECH.values(), *REINFORCEMENT.values()):
        for line in lines:
            assert line not in blob
    assert "all_values" not in blob.lower()
    assert "TEMPORAL ORIENTATION" in request["instructions"]
    assert "CLASSIFICATION" in request["instructions"]
    assert "no_proven_fault" in request["instructions"]
    assert "moving_away" in request["instructions"]
    assert "moving_toward_ball" in request["instructions"]
    assert "P1" not in blob
    assert "shaft parallel" not in blob.lower()
    catalog_text = request["evidence"].split("VISUAL FAULT CATALOG", 1)[1]
    assert "TRAIL_SHOULDER_OUT_EARLY" in catalog_text
    assert "requires_handedness" in catalog_text
    assert "upstream_candidates" not in catalog_text
    assert (
        catalog.FAULTS_BY_ID["TRAIL_SHOULDER_OUT_EARLY"].coaching.drill
        not in catalog_text
    )
    assert "instruction" not in request["schema"]["properties"]
    assert "feel" not in request["schema"]["properties"]
    schema = request["schema"]["properties"]
    assert "fault_candidates" in schema
    assert "handedness" in schema
    assert schema["handedness"]["properties"]["side"]["enum"] == [
        "right",
        "left",
        "uncertain",
    ]
    assert "temporal_orientation" in schema
    assert schema["temporal_orientation"]["items"]["properties"]["state"]["enum"] == [
        "setup",
        "moving_away",
        "direction_change_region",
        "moving_toward_ball",
        "after_strike",
        "uncertain",
    ]
    assert "base64" not in json.dumps(request)
    assert request["image_inputs"] == 16
    assert request["model"] == "gpt-5-mini"
    assert request["reasoning_effort"] == "minimal"
    assert all(image["detail"] == "high" for image in request["images"])
    assert all(image["file"].startswith("diagnostic/") for image in request["images"])
    views = [image["view"] for image in request["images"]]
    assert views[0:2] == ["DTL", "FO"]
    assert request["images"][0]["width"] == 40
    assert request["images"][1]["width"] == 48


def test_send_keeps_the_model_reply_without_rewriting_it(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-b")

    def complete(request: object) -> _Reply:
        assert getattr(request, "reasoning_effort") == "minimal"
        assert all(image.detail == "high" for image in request.images)  # type: ignore[attr-defined]
        return _Reply(_reply_for(request))

    report = dv.execute(
        "shot-b",
        send=True,
        shots_root=shot.parent,
        results_root=tmp_path / "results",
        complete=complete,
    )
    saved = json.loads(
        (tmp_path / "results" / "shot-b" / "reply.json").read_text(encoding="utf-8")
    )
    assert saved["payload"]["status"] == "no_proven_fault"
    assert report["model_classification"]["status"] == "no_proven_fault"
    assert report["caddiej_coaching"] is None
    assert report["primary_selection"]["fault_id"] is None
    assert report["production_reconciliation"] is False
    assert report["validation"]["ok"] is True
    assert report["input_tokens"] == 1000
    assert report["output_tokens"] == 200
    assert report["openai_latency_s"] == 1.25
    assert report["estimated_cost_usd"] is not None
    text = dv.format_summary(report)
    assert "MODEL CLASSIFICATION" in text
    assert "temporal_orientation:" in text
    assert "CADDIEJ COACHING OUTPUT" in text
    assert "\nnone" in text


def test_coaching_comes_from_the_catalog_not_the_model(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-coach")

    def complete(request: object) -> _Reply:
        ticks = _request_ticks(request)
        first = ticks[0]
        payload = _no_fault(ticks, _forward_states(ticks))
        payload["status"] = "classified"
        payload["fault_candidates"] = [
            _candidate(
                "SETUP_WEIGHT_TOWARD_TOES",
                [
                    _see(
                        first,
                        "DTL",
                        "The model wants a different lesson, but this is only an observation.",
                    )
                ],
            )
        ]
        return _Reply(payload)

    report = dv.execute(
        "shot-coach",
        send=True,
        shots_root=shot.parent,
        results_root=tmp_path / "results",
        complete=complete,
    )
    package = catalog.coaching_package("SETUP_WEIGHT_TOWARD_TOES")
    assert report["validation"]["ok"] is True
    assert report["primary_selection"]["fault_id"] == "SETUP_WEIGHT_TOWARD_TOES"
    assert report["caddiej_coaching"] == package
    observation = report["model_classification"]["fault_candidates"][0]["evidence"][0][
        "observation"
    ]
    assert observation not in report["caddiej_coaching"].values()
    assert list(report["caddiej_coaching"]) == [
        "fault_id",
        "name",
        "voice",
        "floor",
        "feel",
        "drill",
        "source",
    ]


def test_exactly_one_coaching_package_is_selected() -> None:
    payload = _setup_fault()
    payload["fault_candidates"].append(
        _candidate(
            "SETUP_ARMS_NOT_HANGING",
            [_see(15, "DTL", "The arms reach away from the body at address.")],
            confidence=0.7,
        )
    )
    problems, chosen, coaching = dv.interpret(payload, _pair_sent(15))
    assert problems == []
    assert chosen is not None and chosen.fault_id == "SETUP_WEIGHT_TOWARD_TOES"
    assert coaching is not None
    assert coaching["fault_id"] == "SETUP_WEIGHT_TOWARD_TOES"
    assert coaching == catalog.coaching_package("SETUP_WEIGHT_TOWARD_TOES")


def test_handedness_right_left_and_uncertain_are_accepted() -> None:
    for side in ("right", "left", "uncertain"):
        assert dv.validate(_no_fault([15], handedness=side), _pair_sent(15)) == []


def test_right_and_left_allow_a_lead_trail_fault() -> None:
    for side in ("right", "left"):
        payload = _shoulder_fault()
        payload["handedness"]["side"] = side
        assert dv.validate(payload, _pair_sent(35, 41)) == []


def test_uncertain_handedness_rejects_a_lead_trail_fault() -> None:
    payload = _shoulder_fault()
    payload["handedness"]["side"] = "uncertain"
    problems = dv.validate(payload, _pair_sent(35, 41))
    assert any("handedness is uncertain" in problem for problem in problems)


def test_uncertain_handedness_allows_a_fault_that_does_not_need_it() -> None:
    payload = _classified(
        "EARLY_EXTENSION",
        [
            _see(15, "DTL", "Address shows the pelvis depth."),
            _see(30, "DTL", "The pelvis has moved closer to the ball."),
        ],
        [15, 30],
        {15: "setup", 30: "moving_toward_ball"},
    )
    payload["handedness"]["side"] = "uncertain"
    assert dv.validate(payload, _pair_sent(15, 30)) == []


def test_missing_handedness_is_rejected() -> None:
    payload = _no_fault([15])
    del payload["handedness"]
    problems = dv.validate(payload, _pair_sent(15))
    assert any("handedness must be" in problem for problem in problems)


def test_no_proven_fault_has_no_coaching_package() -> None:
    problems, selection, coaching = dv.interpret(_no_fault([15]), _pair_sent(15))
    assert problems == []
    assert selection is not None and selection.fault_id is None
    assert coaching is None


def test_failed_validation_does_not_replace_the_reply(tmp_path: Path) -> None:
    shot = _write_shot(tmp_path / "shots" / "shot-c")
    payload = _setup_fault(99)
    payload["fault_candidates"][0]["fault_id"] = "not_a_catalog_fault"

    report = dv.execute(
        "shot-c",
        send=True,
        shots_root=shot.parent,
        results_root=tmp_path / "results",
        complete=lambda _request: _Reply(payload),
    )
    saved = json.loads(
        (tmp_path / "results" / "shot-c" / "reply.json").read_text(encoding="utf-8")
    )
    assert report["validation"]["ok"] is False
    assert saved["payload"]["fault_candidates"][0]["fault_id"] == "not_a_catalog_fault"
    assert report["model_classification"]["fault_candidates"][0]["fault_id"] == (
        "not_a_catalog_fault"
    )
    assert report["caddiej_coaching"] is None
    assert report["primary_selection"] is None
    assert report["production_reconciliation"] is False


def test_cli_does_not_send_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_shot(tmp_path / "shots" / "shot-d")
    monkeypatch.setattr(dv, "SHOTS", tmp_path / "shots")
    monkeypatch.setattr(dv, "RESULTS", tmp_path / "results")

    async def explode(_request: object) -> _Reply:
        raise AssertionError("OpenAI was called without --send")

    monkeypatch.setattr(dv, "send_openai", explode)
    dv.main(["shot-d"])
    assert (tmp_path / "results" / "shot-d" / "selected_frames.json").is_file()
    assert (tmp_path / "results" / "shot-d" / "request.json").is_file()
    assert (tmp_path / "results" / "shot-d" / "report.json").is_file()
    assert not (tmp_path / "results" / "shot-d" / "reply.json").exists()


def test_cli_send_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_shot(tmp_path / "shots" / "shot-e")
    monkeypatch.setattr(dv, "SHOTS", tmp_path / "shots")
    monkeypatch.setattr(dv, "RESULTS", tmp_path / "results")
    seen: list[str] = []

    async def fake(request: object) -> _Reply:
        seen.append(getattr(request, "reasoning_effort"))
        return _Reply(_reply_for(request))

    monkeypatch.setattr(dv, "send_openai", fake)
    dv.main(["shot-e", "--send"])
    assert seen == ["minimal"]


def test_module_does_not_consult_v2_outputs() -> None:
    source = (HERE / "direct_visual_test.py").read_text(encoding="utf-8")
    for banned in (
        "import t36",
        "v2_request",
        "selected_phase",
        "lesson_cue",
        "fresh_v2/runs",
        "fresh_v2/labels",
        "canonical.json",
        "20261008T191545",
        "09132997",
    ):
        assert banned not in source


def test_preserved_shot_has_no_large_replay_gap() -> None:
    """The preserved shot stays inside the generic gap and coverage rules.

    The tick list is whatever those rules produce. It is not a labelled target.
    """

    shot = dv.SHOTS / "20261008T191545-2-09132997"
    if not (shot / "metadata.json").is_file():
        pytest.skip("preserved fresh_v2 shot is not on this machine")
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = dv.plan_selection(meta, shot=shot)
    again = dv.plan_selection(meta, shot=shot)
    ticks = [moment.capture_tick for moment in plan.moments]
    assert ticks == [moment.capture_tick for moment in again.moments]
    assert len(ticks) == 8
    assert ticks == sorted(ticks)
    assert ticks[0] == min(score.capture_tick for score in plan.motion)
    assert ticks[-1] == max(score.capture_tick for score in plan.motion)
    times = [moment.replay_seconds for moment in plan.moments]
    duration = times[-1] - times[0]
    gaps = [right - left for left, right in zip(times, times[1:])]
    assert max(gaps) / duration <= dv.max_gap_fraction(8) + 1e-9
    norms = [(time - times[0]) / duration for time in times]
    for band in range(dv.COVERAGE_BANDS):
        lo = band / dv.COVERAGE_BANDS
        hi = (band + 1) / dv.COVERAGE_BANDS
        assert any(
            (lo <= norm < hi) or (band == dv.COVERAGE_BANDS - 1 and norm == 1.0)
            for norm in norms
        )
    order = [score.capture_tick for score in plan.motion]
    selected = [order.index(tick) for tick in ticks]
    assert any(right - left == 1 for left, right in zip(selected, selected[1:]))
    rates = sorted(score.motion_rate for score in plan.motion)
    assert max(moment.motion_rate for moment in plan.moments) > rates[len(rates) // 2]
    for moment in plan.moments:
        assert [frame.view for frame in moment.frames] == ["DTL", "FO"]
        for frame in moment.frames:
            path = shot / frame.file
            assert path.is_file()
            assert frame.file.startswith("diagnostic/")
            assert frame.width >= 1000 and frame.height >= 1400


def _write_shot(shot: Path) -> Path:
    times = [(index, index * 0.5) for index in range(10)]
    behind, side = _pair(times)
    shot.mkdir(parents=True)
    (shot / "metadata.json").write_text(
        json.dumps(_meta(behind, side)), encoding="utf-8"
    )
    for camera, frames in (("swing_behind", behind), ("swing_side", side)):
        directory = shot / "diagnostic" / camera
        directory.mkdir(parents=True)
        width = 40 if camera == "swing_behind" else 48
        for frame in frames:
            image = Image.new(
                "L", (width, 32), color=(frame["capture_index"] * 17) % 256
            )
            image.save(
                directory / f"frame_{frame['capture_index']:03d}.jpg", format="JPEG"
            )
    return shot
