"""Stage-3 contracts and the pose-free stage estimator.

The estimator is exercised on synthetic trajectories. It does not read the
development swings, and it does not call a model.
"""

from __future__ import annotations

import json
import math
from dataclasses import fields
from pathlib import Path

import pytest
import swing_stage_detector as detector
from contracts import (
    SCHEMA_VERSION,
    STATUS_INSUFFICIENT,
    STATUS_PARTIAL,
    AssessmentFinding,
    CameraCapture,
    CaptureResult,
    CoachingDecision,
    CoachingDeliveryResult,
    FrameReference,
    Provenance,
    SampledFrame,
    TemporalSamplingResult,
    VisualAssessmentResult,
    require_header,
    to_plain,
)
from swing_stage_detector import (
    PARAMETERS,
    LandmarkObservation,
    PoseFrame,
    detect_swing_stages,
    load_camera_frames,
    render_overlay,
)

HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / "swing_stage_detector.py"
CONTRACTS = HERE / "contracts.py"


def _point(x: float, y: float, visibility: float = 0.95) -> LandmarkObservation:
    return LandmarkObservation(x=x, y=y, visibility=visibility, presence=visibility)


def _frame(
    tick: int,
    time_seconds: float,
    elevation: float,
    *,
    visibility: float = 0.95,
    torso: float = 0.18,
    missing: frozenset[str] = frozenset(),
    low_visibility: frozenset[str] = frozenset(),
    wrist_y_offset: float = 0.0,
) -> PoseFrame:
    hip_y = 0.62
    shoulder_y = hip_y - torso
    wrist_y = hip_y - elevation * torso + wrist_y_offset
    elbow_y = (shoulder_y + wrist_y) / 2.0
    placed = {
        "left_shoulder": (0.46, shoulder_y),
        "right_shoulder": (0.54, shoulder_y),
        "left_elbow": (0.47, elbow_y),
        "right_elbow": (0.53, elbow_y),
        "left_wrist": (0.49, wrist_y),
        "right_wrist": (0.51, wrist_y),
        "left_hip": (0.47, hip_y),
        "right_hip": (0.53, hip_y),
    }
    landmarks = {}
    for name, (x, y) in placed.items():
        if name in missing:
            continue
        point_visibility = 0.2 if name in low_visibility else visibility
        landmarks[name] = _point(x, y, point_visibility)
    return PoseFrame(
        capture_tick=tick,
        timestamp_seconds=time_seconds,
        pose_detected=True,
        pose_presence=0.9,
        landmarks=landmarks,
    )


def _shape(time_seconds: float) -> float:
    """A generic rise, reversal, descent, and recovery. The peak is at 1.0 s."""

    if time_seconds <= 0.40:
        return -0.20
    if time_seconds <= 1.0:
        return -0.20 + 1.90 * (time_seconds - 0.40) / 0.60
    if time_seconds <= 1.45:
        return 1.70 - 1.85 * (time_seconds - 1.0) / 0.45
    if time_seconds <= 1.85:
        return -0.15 + 0.95 * (time_seconds - 1.45) / 0.40
    return 0.80


def _series(
    times: list[float],
    *,
    noise: float = 0.0,
    visibility: float = 0.95,
    missing_until: float | None = None,
    wrist_y_offset: float = 0.0,
    low_wrists: bool = False,
    shape_origin: float = 0.0,
) -> list[PoseFrame]:
    frames = []
    low = frozenset({"left_wrist", "right_wrist"}) if low_wrists else frozenset()
    for index, time_seconds in enumerate(times):
        elevation = _shape(time_seconds - shape_origin) + noise * math.sin(index * 1.7)
        missing = (
            frozenset({"left_wrist", "right_wrist"})
            if missing_until is not None and time_seconds < missing_until
            else frozenset()
        )
        frames.append(
            _frame(
                index + 1,
                time_seconds,
                elevation,
                visibility=visibility,
                missing=missing,
                low_visibility=low,
                wrist_y_offset=wrist_y_offset,
            )
        )
    return frames


def _uniform(scale: float = 1.0, shift: float = 0.0) -> list[float]:
    times = []
    time_seconds = 0.0
    while time_seconds <= 2.60 * scale + 1e-9:
        times.append(shift + time_seconds)
        time_seconds += 0.10 * scale
    return times


def _scaled_shape_frames(scale: float) -> list[PoseFrame]:
    """The same motion stretched in time. Elevations follow unscaled shape time."""

    frames = []
    time_seconds = 0.0
    index = 0
    while time_seconds <= 2.60 * scale + 1e-9:
        frames.append(_frame(index + 1, time_seconds, _shape(time_seconds / scale)))
        index += 1
        time_seconds += 0.10 * scale
    return frames


def _transition(result):
    return next(item for item in result.boundaries if item.stage == "TRANSITION")


def test_stage_contracts_share_a_header_and_do_not_carry_pixels() -> None:
    provenance = Provenance("capture", "test", "1")
    reference = FrameReference(
        "swing_behind", "diagnostic/swing_behind/frame_001.jpg", 1, 0.5
    )
    capture = CaptureResult(
        SCHEMA_VERSION,
        "shot-a",
        "ok",
        provenance,
        "cap-1",
        (CameraCapture("swing_behind", (reference,)),),
    )
    sampled = TemporalSamplingResult(
        SCHEMA_VERSION,
        "shot-a",
        "ok",
        Provenance("temporal_sampling", "test", "1"),
        "swing_behind",
        (SampledFrame(reference, 0, "motion"),),
    )
    assessment = VisualAssessmentResult(
        SCHEMA_VERSION,
        "shot-a",
        "partial",
        Provenance("visual_assessment", "test", "1"),
        (
            AssessmentFinding(
                "weakness",
                "STEEP_DELIVERY",
                0.4,
                "uncalibrated_heuristic",
                (reference,),
                ("DOWNSWING",),
                "example",
            ),
        ),
        False,
        SCHEMA_VERSION,
        ("IMPACT_REGION",),
    )
    decision = CoachingDecision(
        SCHEMA_VERSION,
        "shot-a",
        "ok",
        Provenance("coaching_decision", "test", "1"),
        "focus",
        "STEEP_DELIVERY",
        "cue-steep",
        SCHEMA_VERSION,
    )
    delivery = CoachingDeliveryResult(
        SCHEMA_VERSION,
        "shot-a",
        "ok",
        Provenance("coaching_delivery", "test", "1"),
        SCHEMA_VERSION,
        True,
        ("floor", "speech"),
        None,
    )
    for result in (capture, sampled, assessment, decision, delivery):
        payload = to_plain(result)
        require_header(payload)
        assert payload["schema_version"] == SCHEMA_VERSION
        encoded = json.dumps(payload)
        assert "bytes" not in payload
        assert isinstance(encoded, str)
    assert "stage" not in {item.name for item in fields(SampledFrame)}
    assert "boundaries" not in {item.name for item in fields(VisualAssessmentResult)}
    assert "corrected_stages" not in {
        item.name for item in fields(VisualAssessmentResult)
    }
    assessment_fields = {item.name for item in fields(VisualAssessmentResult)}
    assert "findings" in assessment_fields
    assert "focus_fault_id" not in {
        item.name for item in fields(CoachingDeliveryResult)
    }
    assert "cue_text" not in {item.name for item in fields(CoachingDecision)}


def test_direction_reversal_is_a_partial_result_without_impact() -> None:
    result = detect_swing_stages(_series(_uniform()), shot_id="synthetic")
    payload = to_plain(result)
    require_header(payload)
    assert result.status == STATUS_PARTIAL
    assert result.orientation == "estimated"
    assert result.impact.supported is False
    assert "IMPACT_REGION" in result.unresolved_stages
    assert all(item.stage != "IMPACT_REGION" for item in result.boundaries)
    assert all(item.stage != "IMPACT_REGION" for item in result.candidates)
    transition = _transition(result)
    peak = result.hand_excursion.peak_time_seconds
    assert peak is not None
    assert abs(peak - 1.0) < 0.15
    start, end = transition.time_interval_seconds
    assert start <= peak <= end
    assert transition.confidence >= PARAMETERS.accept_confidence
    assert transition.confidence < 1.0
    assert transition.confidence_scale == "uncalibrated_heuristic"
    assert "probability" not in transition.confidence_scale
    names = [item.stage for item in result.boundaries]
    assert names == sorted(names, key=lambda name: detector.TAXONOMY.index(name))
    ticks = [tick for item in result.boundaries for tick in item.evidence_ticks]
    assert len(ticks) == len(set(ticks))
    by_tick = {
        frame.capture_tick: frame.timestamp_seconds for frame in _series(_uniform())
    }
    for boundary in result.boundaries:
        times = [by_tick[tick] for tick in boundary.evidence_ticks]
        assert times == sorted(times)
        assert boundary.time_interval_seconds[0] == times[0]
        assert boundary.time_interval_seconds[1] == times[-1]


def test_estimates_are_deterministic_and_follow_the_motion() -> None:
    frames = _series(_uniform())
    first = to_plain(detect_swing_stages(frames, shot_id="synthetic"))
    second = to_plain(detect_swing_stages(frames, shot_id="synthetic"))
    assert first == second
    early = detect_swing_stages(_series(_uniform(shift=0.0)), shot_id="early")
    late = detect_swing_stages(
        _series(_uniform(shift=3.0), shape_origin=3.0), shot_id="late"
    )
    early_peak = early.hand_excursion.peak_time_seconds
    late_peak = late.hand_excursion.peak_time_seconds
    assert early_peak is not None and late_peak is not None
    assert abs((late_peak - early_peak) - 3.0) < 0.05
    assert abs(early_peak - 1.0) < 0.15
    assert abs(late_peak - 4.0) < 0.15
    stretched = detect_swing_stages(_scaled_shape_frames(2.0), shot_id="slow")
    stretched_peak = stretched.hand_excursion.peak_time_seconds
    assert stretched_peak is not None
    assert abs(stretched_peak - 2.0) < 0.25
    assert early.parameters == late.parameters == stretched.parameters == PARAMETERS
    behind = detect_swing_stages(frames, shot_id="synthetic", camera_id="swing_behind")
    other = detect_swing_stages(frames, shot_id="synthetic", camera_id="other_camera")
    assert behind.boundaries == other.boundaries
    assert other.camera_id == "other_camera"


def test_non_uniform_timestamps_follow_the_clock_not_the_index() -> None:
    times = [0.0]
    gaps = (0.05, 0.16, 0.07, 0.18, 0.04, 0.15)
    while times[-1] < 2.60:
        times.append(round(times[-1] + gaps[(len(times) - 1) % len(gaps)], 4))
    if 1.0 not in times:
        times.append(1.0)
        times.sort()
    result = detect_swing_stages(_series(times), shot_id="uneven")
    peak = result.hand_excursion.peak_time_seconds
    assert peak is not None
    assert abs(peak - 1.0) < 0.15
    clip_mid = (times[0] + times[-1]) / 2.0
    assert abs(peak - clip_mid) > 0.2
    start, end = _transition(result).time_interval_seconds
    assert start <= peak <= end


def test_no_reversal_and_a_small_excursion_are_not_accepted() -> None:
    rising = [
        _frame(index + 1, index * 0.1, -0.2 + index * 0.08) for index in range(20)
    ]
    monotonic = detect_swing_stages(rising, shot_id="rise-only")
    assert monotonic.status == STATUS_INSUFFICIENT
    assert monotonic.boundaries == ()
    assert monotonic.hand_excursion.observed is False
    assert monotonic.orientation == "unknown"
    assert "IMPACT_REGION" in monotonic.unresolved_stages

    small = [_frame(index + 1, index * 0.1, _small(index * 0.1)) for index in range(20)]
    weak = detect_swing_stages(small, shot_id="small")
    assert weak.boundaries == ()
    assert weak.status == STATUS_INSUFFICIENT
    assert weak.hand_excursion.gates_passed is False
    assert weak.impact.supported is False


def _small(time_seconds: float) -> float:
    if time_seconds <= 0.4:
        return -0.2
    if time_seconds <= 1.0:
        return -0.2 + 0.40 * (time_seconds - 0.4) / 0.6
    if time_seconds <= 1.4:
        return 0.20 - 0.40 * (time_seconds - 1.0) / 0.4
    return -0.2


def test_missing_and_low_visibility_landmarks_are_not_invented() -> None:
    gapped = _series(_uniform(), missing_until=0.35)
    result = detect_swing_stages(gapped, shot_id="gapped")
    assert result.hand_excursion.peak_time_seconds is not None
    assert abs(result.hand_excursion.peak_time_seconds - 1.0) < 0.15
    assert result.tracking is not None
    wrist_gaps = [
        item
        for item in result.tracking.missing_landmark_intervals
        if item.landmark == "left_wrist"
    ]
    assert wrist_gaps
    assert wrist_gaps[0].frame_count >= 1
    low_a = _series(_uniform(), low_wrists=True, wrist_y_offset=0.0)
    low_b = _series(_uniform(), low_wrists=True, wrist_y_offset=0.4)
    result_a = detect_swing_stages(low_a, shot_id="low")
    result_b = detect_swing_stages(low_b, shot_id="low")
    assert result_a.boundaries == ()
    assert result_b.boundaries == ()
    assert result_a.hand_excursion == result_b.hand_excursion
    assert result_a.hand_excursion.observed is False
    assert to_plain(result_a) == to_plain(result_b)


def test_chronological_order_and_incomplete_input() -> None:
    frames = _series(_uniform())
    reversed_frames = list(reversed(frames))
    forward = detect_swing_stages(frames, shot_id="order")
    backward = detect_swing_stages(reversed_frames, shot_id="order")
    assert forward.boundaries == backward.boundaries
    assert any("not chronological" in warning for warning in backward.warnings)
    short = detect_swing_stages(frames[:2], shot_id="short")
    assert short.status == STATUS_INSUFFICIENT
    assert short.boundaries == ()
    assert short.errors == ()
    duplicated = [
        frames[0],
        replace_time(frames[1], frames[0].timestamp_seconds),
        *frames[2:],
    ]
    detect_swing_stages(duplicated, shot_id="duplicate")
    with pytest.raises(ValueError, match="shot_id"):
        detect_swing_stages(frames, shot_id="")


def replace_time(frame: PoseFrame, time_seconds: float) -> PoseFrame:
    return PoseFrame(
        capture_tick=frame.capture_tick,
        timestamp_seconds=time_seconds,
        pose_detected=frame.pose_detected,
        pose_presence=frame.pose_presence,
        landmarks=frame.landmarks,
    )


def test_noisy_trajectory_keeps_the_same_reversal() -> None:
    clean = detect_swing_stages(_series(_uniform()), shot_id="clean")
    noisy = detect_swing_stages(_series(_uniform(), noise=0.05), shot_id="noisy")
    clean_peak = clean.hand_excursion.peak_time_seconds
    noisy_peak = noisy.hand_excursion.peak_time_seconds
    assert clean_peak is not None and noisy_peak is not None
    assert abs(noisy_peak - clean_peak) <= 0.2
    assert noisy.boundaries
    again = detect_swing_stages(_series(_uniform(), noise=0.05), shot_id="noisy")
    assert to_plain(noisy) == to_plain(again)


def test_accept_bar_is_shared_and_not_a_probability() -> None:
    assert PARAMETERS.accept_confidence == 0.60
    result = detect_swing_stages(_series(_uniform()), shot_id="bar")
    assert result.confidence_scale == "uncalibrated_heuristic"
    transition = _transition(result)
    assert transition.confidence >= PARAMETERS.accept_confidence
    assert any("not probabilities" in warning for warning in result.warnings)
    assert any("Handedness is unknown" in warning for warning in result.warnings)
    assert any("not impact" in warning for warning in result.warnings)


def test_loader_uses_offset_seconds_and_ignores_strike_fields(tmp_path: Path) -> None:
    shot = tmp_path / "20261008T191451-1-93738c0a"
    directory = shot / "diagnostic" / "swing_behind"
    directory.mkdir(parents=True)
    rows = []
    for tick, offset in ((3, 1.2), (1, 0.4), (2, 0.8)):
        relative = f"diagnostic/swing_behind/frame_{tick:03d}.jpg"
        (shot / relative).write_bytes(b"not-a-real-jpeg")
        rows.append(
            {
                "capture_index": tick,
                "file": relative,
                "offset_seconds": offset,
                "offset_from_strike_seconds": 41.0,
                "offset_from_replay_start_seconds": 2.0,
                "selected_phase": "impact",
                "selected": True,
            }
        )
    payload = {
        "capture_id": "abc",
        "shot_id": None,
        "diagnostic_timeline": {
            "replay_start_seconds": 1.9,
            "cameras": [
                {"camera_id": "swing_behind", "view": "behind", "frames": rows}
            ],
        },
    }
    (shot / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_camera_frames(shot, "swing_behind")
    assert [frame.capture_tick for frame in loaded] == [1, 2, 3]
    assert [frame.timestamp_seconds for frame in loaded] == [0.4, 0.8, 1.2]
    escaped = dict(rows[0])
    escaped["file"] = "diagnostic/swing_behind/../../metadata.json"
    payload["diagnostic_timeline"]["cameras"][0]["frames"] = [escaped]
    (shot / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="no readable"):
        load_camera_frames(shot, "swing_behind")


def test_overlay_does_not_modify_the_source_image() -> None:
    from PIL import Image

    image = Image.new("RGB", (40, 60), "black")
    before = image.tobytes()
    frame = _frame(1, 0.0, 0.2)
    overlaid = render_overlay(image, frame)
    assert image.tobytes() == before
    assert overlaid is not image
    assert overlaid.tobytes() != before


def test_working_set_is_measurable_on_windows() -> None:
    import os

    if os.name != "nt":
        pytest.skip("working set is read from the Windows process API")
    current, peak = detector._working_set()
    assert current is not None and current > 0
    assert peak is not None and peak >= current


def test_detector_source_has_no_model_call_and_no_contact_clock() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    contracts = CONTRACTS.read_text(encoding="utf-8")
    combined = text + "\n" + contracts
    lowered = combined.lower()
    assert "openai" not in lowered
    assert "api.openai.com" not in lowered
    for banned in (
        "offset_from_strike",
        "offset_from_replay",
        "selected_phase",
        "replay_start_seconds",
        "temporal_sampler",
        "simple_visual_classifier",
        "fault_catalog",
        "lead_wrist",
        "trail_wrist",
    ):
        assert banned not in combined
    imports = [
        line
        for line in text.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert all("caddiej" not in line for line in imports)
    assert "1.893" not in text
    assert "phases.py" not in text
