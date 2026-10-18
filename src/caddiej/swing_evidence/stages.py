"""Swing-stage detection from a local pose model.

The input is a chronological camera sequence and the timestamps stored with
it. The output is a swing-stage result. It does not sample frames, name
faults, or choose coaching.

Handedness is unknown. Left and right are the person's sides as labeled by
the pose model, not lead and trail. Ball contact is not inferred: a high
hand, a change of direction, and a fast descent are not impact.

The pose backend is MediaPipe Pose Landmarker in video mode. Joint centers
on a down-the-line frame are model estimates. A landmark below the visibility
floor is missing and is not filled in.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\swing_stage_detector.py \\
        <shot-id> [<shot-id> ...]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from caddiej.swing_evidence.contracts import (
    CONFIDENCE_SCALE,
    SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_INSUFFICIENT,
    STATUS_PARTIAL,
    ConfidenceComponent,
    HandExcursion,
    ImpactObservation,
    LandmarkContinuity,
    MissingInterval,
    Provenance,
    StageBoundary,
    StageParameters,
    SwingStageResult,
    TrackingSummary,
    to_plain,
)
from caddiej.swing_evidence.pose_model import ensure_pose_model
from caddiej.swing_evidence.ready import configure_matplotlib

DETECTOR_VERSION = "1"
CLOCK = "diagnostic_timeline.offset_seconds"
HANDEDNESS_UNKNOWN = "unknown"
POSE_BACKEND = "mediapipe_pose_landmarker_video"
POSE_MODEL_NAME = "pose_landmarker_full_float16"
_REPO = Path(__file__).resolve().parents[3]
REPO = _REPO
DEFAULT_SHOTS = REPO / "research" / "temporal_evidence" / "fresh_v2" / "shots"
DEFAULT_RESULTS = (
    REPO / "research" / "direct_visual_coach" / "results" / "swing_stage_detector"
)

configure_matplotlib()

# Anatomical gates, identical for every shot. Distances are torso lengths:
# wrist height above the hip midpoint, divided by shoulder-to-hip distance.
# Confidence weights are a heuristic. They are not a calibrated probability.
PARAMETERS = StageParameters(
    visibility_min=0.5,
    smooth_radius_seconds=0.12,
    max_gap_seconds=0.25,
    max_elevation_jump_torso=3.0,
    min_torso_fraction=0.04,
    min_rise_torso=0.75,
    min_fall_torso=0.50,
    peak_band_fraction=0.12,
    min_follow_rise_torso=0.35,
    accept_confidence=0.60,
    min_run_samples=5,
    setup_min_seconds=0.20,
    setup_max_range_torso=0.30,
    finish_min_seconds=0.25,
    finish_max_range_torso=0.25,
    finish_max_speed_torso_per_second=0.50,
)

TAXONOMY = (
    "SETUP",
    "BACKSWING",
    "TRANSITION",
    "DOWNSWING",
    "IMPACT_REGION",
    "FOLLOW_THROUGH",
    "FINISH",
)
LANDMARKS = (
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
)
# MediaPipe pose-landmark indexes. Hips are 23 and 24, not the next integers
# after the wrists.
_LANDMARK_INDEX = {
    "left_shoulder": 11,
    "right_shoulder": 12,
    "left_elbow": 13,
    "right_elbow": 14,
    "left_wrist": 15,
    "right_wrist": 16,
    "left_hip": 23,
    "right_hip": 24,
}

_COLORS = {
    "left_shoulder": (30, 140, 30),
    "right_shoulder": (80, 190, 80),
    "left_elbow": (40, 90, 210),
    "right_elbow": (200, 50, 50),
    "left_wrist": (0, 170, 210),
    "right_wrist": (230, 120, 20),
    "left_hip": (150, 40, 170),
    "right_hip": (210, 90, 190),
}
_STAGE_COLORS = {
    "SETUP": (190, 190, 190),
    "BACKSWING": (120, 170, 220),
    "TRANSITION": (240, 180, 60),
    "DOWNSWING": (220, 90, 90),
    "FOLLOW_THROUGH": (120, 190, 140),
    "FINISH": (160, 140, 200),
}

_WARNING_CONFIDENCE = (
    "Confidence values are uncalibrated heuristics, not probabilities."
)
_WARNING_HAND = (
    "Handedness is unknown. Left and right are the person's sides from the "
    "pose model, not lead and trail."
)
_WARNING_IMPACT = (
    "IMPACT_REGION is unresolved. Ball contact is not observable from hand "
    "elevation. A hand-height peak, a direction change, and a fast descent "
    "are not impact."
)
_WARNING_JOINTS = (
    "Pose joint centers are model estimates. On a non-square down-the-line "
    "frame, visibility is not positional accuracy."
)
_IMPACT_REASON = (
    "No observation of ball-club contact. Hand elevation does not establish "
    "an impact region."
)


@dataclass(frozen=True, slots=True)
class LandmarkObservation:
    x: float
    y: float
    visibility: float | None
    presence: float | None = None


@dataclass(frozen=True, slots=True)
class PoseFrame:
    """One frame after pose inference. Missing landmarks are omitted."""

    capture_tick: int
    timestamp_seconds: float
    pose_detected: bool
    pose_presence: float | None
    landmarks: dict[str, LandmarkObservation]
    relative_file: str = ""
    image_path: str = ""
    width: int = 0
    height: int = 0
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class CameraFrame:
    capture_tick: int
    path: Path
    relative_file: str
    timestamp_seconds: float
    width: int
    height: int
    sha256: str
    rgb: np.ndarray | None = None


@dataclass(frozen=True, slots=True)
class _Sample:
    frame_index: int
    tick: int
    time_seconds: float
    elevation: float | None
    torso: float | None
    wrist_visibility: float | None
    wrists_used: int
    points: dict[str, tuple[float, float]]


@dataclass(frozen=True, slots=True)
class _Peak:
    samples: tuple[_Sample, ...]
    smoothed: tuple[float, ...]
    velocity: tuple[float | None, ...]
    peak: int
    rise: float
    fall: float
    reversal: bool

    @property
    def gates_passed(self) -> bool:
        return (
            len(self.samples) >= PARAMETERS.min_run_samples
            and self.rise >= PARAMETERS.min_rise_torso
            and self.fall >= PARAMETERS.min_fall_torso
            and self.reversal
        )


@dataclass(frozen=True, slots=True)
class _Analysis:
    result: SwingStageResult
    frames: tuple[PoseFrame, ...]
    samples: tuple[_Sample, ...]
    smoothed_by_tick: dict[int, float]


def detect_swing_stages(
    frames: Sequence[PoseFrame],
    *,
    shot_id: str,
    camera_id: str = "swing_behind",
) -> SwingStageResult:
    """Estimate stage boundaries from pose observations.

    `camera_id` is recorded and is not used to choose a handedness or a clock.
    """

    return _analyze(frames, shot_id=shot_id, camera_id=camera_id).result


def _analyze(
    frames: Sequence[PoseFrame],
    *,
    shot_id: str,
    camera_id: str,
) -> _Analysis:
    if not isinstance(shot_id, str) or not shot_id:
        raise ValueError("shot_id is required")
    ordered, warnings = _prepare(frames)
    samples = tuple(_sample(index, frame) for index, frame in enumerate(ordered))
    peak = _best_peak(samples)
    smoothed = _smoothed_map(samples)
    boundaries: list[StageBoundary] = []
    candidates: list[StageBoundary] = []
    if peak is not None and not peak.gates_passed:
        warnings.append(
            "Largest hand excursion did not meet the swing gates: "
            f"rise={peak.rise:.2f} fall={peak.fall:.2f} torso lengths, "
            f"reversal={str(peak.reversal).lower()}."
        )
    elif peak is None:
        warnings.append("No interior rise and fall of the hands was found.")
    if peak is not None and peak.gates_passed:
        bundle, extra = _bundle(peak)
        warnings.extend(extra)
        transition = next(item for item in bundle if item.stage == "TRANSITION")
        if transition.confidence >= PARAMETERS.accept_confidence:
            boundaries = bundle
        else:
            candidates = bundle
            warnings.append(
                "A direction change was found, and its heuristic confidence "
                f"{transition.confidence:.2f} is below {PARAMETERS.accept_confidence:.2f}. "
                "The intervals are candidates, not accepted stages."
            )
    warnings.extend((_WARNING_CONFIDENCE, _WARNING_HAND, _WARNING_IMPACT))
    accepted = {item.stage for item in boundaries}
    unresolved = tuple(stage for stage in TAXONOMY if stage not in accepted)
    status = STATUS_PARTIAL if boundaries else STATUS_INSUFFICIENT
    result = SwingStageResult(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=status,
        provenance=_provenance(),
        camera_id=camera_id,
        clock=CLOCK,
        handedness=HANDEDNESS_UNKNOWN,
        orientation="estimated" if boundaries else "unknown",
        confidence_scale=CONFIDENCE_SCALE,
        parameters=PARAMETERS,
        boundaries=tuple(boundaries),
        candidates=tuple(candidates),
        unresolved_stages=unresolved,
        tracking=_tracking(ordered),
        hand_excursion=_excursion(peak),
        impact=ImpactObservation(supported=False, reason=_IMPACT_REASON),
        warnings=tuple(warnings),
        errors=(),
    )
    return _Analysis(
        result=result,
        frames=tuple(ordered),
        samples=samples,
        smoothed_by_tick=smoothed,
    )


def _provenance() -> Provenance:
    return Provenance(
        stage="swing_stage_detection",
        producer="swing_stage_detector",
        producer_version=DETECTOR_VERSION,
    )


def _failed(
    shot_id: str,
    camera_id: str,
    error: str,
    *,
    warnings: tuple[str, ...] = (),
) -> SwingStageResult:
    return SwingStageResult(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=STATUS_FAILED,
        provenance=_provenance(),
        camera_id=camera_id,
        clock=CLOCK,
        handedness=HANDEDNESS_UNKNOWN,
        orientation="unknown",
        confidence_scale=CONFIDENCE_SCALE,
        parameters=PARAMETERS,
        boundaries=(),
        candidates=(),
        unresolved_stages=TAXONOMY,
        tracking=None,
        hand_excursion=HandExcursion(
            observed=False,
            rise_torso_lengths=None,
            fall_torso_lengths=None,
            peak_time_seconds=None,
            peak_tick=None,
            wrists_at_peak=None,
            gates_passed=False,
            reversal=False,
        ),
        impact=ImpactObservation(supported=False, reason="not evaluated"),
        warnings=warnings,
        errors=(error,),
    )


def _prepare(frames: Sequence[PoseFrame]) -> tuple[list[PoseFrame], list[str]]:
    warnings: list[str] = []
    finite: list[PoseFrame] = []
    dropped = 0
    for frame in frames:
        timestamp = frame.timestamp_seconds
        if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
            dropped += 1
            continue
        if timestamp != timestamp or timestamp in (float("inf"), float("-inf")):
            dropped += 1
            continue
        finite.append(frame)
    if dropped:
        warnings.append(f"{dropped} frames had a non-finite timestamp and were ignored")
    indexed = list(enumerate(finite))
    ordered = [
        frame
        for _, frame in sorted(
            indexed,
            key=lambda item: (
                item[1].timestamp_seconds,
                item[1].capture_tick,
                item[0],
            ),
        )
    ]
    times = [frame.timestamp_seconds for frame in finite]
    if times != sorted(times):
        warnings.append("frames were not chronological; sorted by timestamp and tick")
    for earlier, later in zip(ordered, ordered[1:]):
        if later.timestamp_seconds <= earlier.timestamp_seconds:
            warnings.append(
                "two frames share a timestamp; velocity is not taken across that pair"
            )
            break
    return ordered, warnings


def _usable(point: LandmarkObservation | None) -> LandmarkObservation | None:
    if point is None or point.visibility is None:
        return None
    if point.visibility < PARAMETERS.visibility_min:
        return None
    return point


def _mid(
    left: LandmarkObservation | None, right: LandmarkObservation | None
) -> tuple[float, float] | None:
    if left is None or right is None:
        return None
    return ((left.x + right.x) / 2.0, (left.y + right.y) / 2.0)


def _sample(index: int, frame: PoseFrame) -> _Sample:
    points = frame.landmarks
    shoulders = _mid(
        _usable(points.get("left_shoulder")), _usable(points.get("right_shoulder"))
    )
    hips = _mid(_usable(points.get("left_hip")), _usable(points.get("right_hip")))
    wrists = [
        point
        for point in (
            _usable(points.get("left_wrist")),
            _usable(points.get("right_wrist")),
        )
        if point is not None
    ]
    plotted: dict[str, tuple[float, float]] = {}
    for name in LANDMARKS:
        point = _usable(points.get(name))
        if point is not None:
            plotted[name] = (point.x, point.y)
    elevation = None
    torso = None
    visibility = None
    if shoulders is not None and hips is not None and wrists:
        torso = math.hypot(shoulders[0] - hips[0], shoulders[1] - hips[1])
        if torso >= PARAMETERS.min_torso_fraction:
            wrist_y = sum(point.y for point in wrists) / len(wrists)
            # Image y increases downward, so this is positive when the hands
            # are above the hips.
            elevation = (hips[1] - wrist_y) / torso
            visibility = sum(point.visibility or 0.0 for point in wrists) / len(wrists)
        else:
            torso = None
    return _Sample(
        frame_index=index,
        tick=frame.capture_tick,
        time_seconds=float(frame.timestamp_seconds),
        elevation=elevation,
        torso=torso,
        wrist_visibility=visibility,
        wrists_used=len(wrists) if elevation is not None else 0,
        points=plotted,
    )


def _runs(samples: Sequence[_Sample]) -> list[list[_Sample]]:
    runs: list[list[_Sample]] = []
    current: list[_Sample] = []
    for sample in samples:
        if sample.elevation is None:
            continue
        if current:
            previous = current[-1]
            elapsed = sample.time_seconds - previous.time_seconds
            jump = abs((sample.elevation or 0.0) - (previous.elevation or 0.0))
            if (
                elapsed > PARAMETERS.max_gap_seconds
                or elapsed <= 0.0
                or jump > PARAMETERS.max_elevation_jump_torso
            ):
                runs.append(current)
                current = []
        current.append(sample)
    if current:
        runs.append(current)
    return runs


def _smooth(times: Sequence[float], values: Sequence[float]) -> list[float]:
    radius = PARAMETERS.smooth_radius_seconds
    smoothed: list[float] = []
    for index, time_seconds in enumerate(times):
        total = 0.0
        weight = 0.0
        for other, other_time in enumerate(times):
            delta = abs(other_time - time_seconds)
            if delta > radius:
                continue
            sample_weight = 1.0 if radius <= 0.0 else 1.0 - delta / radius
            total += sample_weight * values[other]
            weight += sample_weight
        smoothed.append(values[index] if weight <= 0.0 else total / weight)
    return smoothed


def _velocity(times: Sequence[float], values: Sequence[float]) -> list[float | None]:
    count = len(times)
    velocity: list[float | None] = [None] * count
    for index in range(count):
        if index == 0 and count > 1:
            left, right = 0, 1
        elif index == count - 1 and count > 1:
            left, right = count - 2, count - 1
        elif 0 < index < count - 1:
            left, right = index - 1, index + 1
        else:
            continue
        elapsed = times[right] - times[left]
        if elapsed > 0.0:
            velocity[index] = (values[right] - values[left]) / elapsed
    return velocity


def _local_peaks(values: Sequence[float]) -> list[int]:
    peaks: list[int] = []
    last = len(values) - 1
    index = 1
    while index < last:
        if values[index] < values[index - 1]:
            index += 1
            continue
        end = index
        while end < last and values[end + 1] >= values[end]:
            end += 1
        if (
            end < last
            and values[end] > values[end + 1]
            and values[end] > values[index - 1]
        ):
            start = end
            while start > 0 and values[start - 1] == values[end]:
                start -= 1
            peaks.append((start + end) // 2)
        index = end + 1
    return peaks


def _best_peak(samples: Sequence[_Sample]) -> _Peak | None:
    best: _Peak | None = None
    best_key: tuple[float, float, float] | None = None
    for run in _runs(samples):
        if len(run) < 3:
            continue
        times = [sample.time_seconds for sample in run]
        values = _smooth(times, [sample.elevation or 0.0 for sample in run])
        velocity = _velocity(times, values)
        for peak in _local_peaks(values):
            rise = values[peak] - min(values[:peak])
            fall = values[peak] - min(values[peak + 1 :])
            reversal = any(
                item is not None and item > 0.0 for item in velocity[:peak]
            ) and any(item is not None and item < 0.0 for item in velocity[peak + 1 :])
            choice = _Peak(
                samples=tuple(run),
                smoothed=tuple(values),
                velocity=tuple(velocity),
                peak=peak,
                rise=rise,
                fall=fall,
                reversal=reversal,
            )
            # Prefer the excursion that goes up and comes back down, then the
            # earlier one. The absolute height is not the score: a high finish
            # is not the top.
            key = (min(rise, fall), rise + fall, -times[peak])
            if best_key is None or key > best_key:
                best = choice
                best_key = key
    return best


def _smoothed_map(samples: Sequence[_Sample]) -> dict[int, float]:
    mapped: dict[int, float] = {}
    for run in _runs(samples):
        times = [sample.time_seconds for sample in run]
        values = _smooth(times, [sample.elevation or 0.0 for sample in run])
        for sample, value in zip(run, values):
            mapped[sample.tick] = value
    return mapped


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _cap(value: float) -> float:
    return min(0.99, max(0.0, value))


def _transition_confidence(
    peak: _Peak, start: int, end: int
) -> tuple[float, tuple[ConfidenceComponent, ...]]:
    window = peak.samples[start : end + 1]
    visibilities = [
        sample.wrist_visibility
        for sample in window
        if sample.wrist_visibility is not None
    ]
    visibility = sum(visibilities) / len(visibilities) if visibilities else 0.0
    both = (
        sum(1 for sample in window if sample.wrists_used >= 2) / len(window)
        if window
        else 0.0
    )
    excursion = min(1.0, peak.rise / 1.0) * min(1.0, peak.fall / 0.75)
    reversal = 1.0 if peak.reversal else 0.0
    times = [sample.time_seconds for sample in peak.samples]
    edge = (
        1.0
        if times[peak.peak] - times[0] >= 0.20 and times[-1] - times[peak.peak] >= 0.20
        else 0.5
    )
    raw = (0.25 * visibility + 0.15 * both + 0.35 * excursion + 0.25 * reversal) * edge
    components = (
        ConfidenceComponent("visibility", visibility),
        ConfidenceComponent("both_wrists", both),
        ConfidenceComponent("excursion", excursion),
        ConfidenceComponent("reversal", reversal),
        ConfidenceComponent("edge_support", edge),
    )
    return _cap(raw), components


def _interval(
    stage: str,
    samples: Sequence[_Sample],
    start: int,
    end: int,
    confidence: float,
    method: str,
    components: tuple[ConfidenceComponent, ...] = (),
) -> StageBoundary:
    chunk = samples[start : end + 1]
    return StageBoundary(
        stage=stage,
        time_interval_seconds=(chunk[0].time_seconds, chunk[-1].time_seconds),
        confidence=confidence,
        confidence_scale=CONFIDENCE_SCALE,
        evidence_ticks=tuple(sample.tick for sample in chunk),
        method=method,
        components=components,
    )


def _bundle(peak: _Peak) -> tuple[list[StageBoundary], list[str]]:
    values = peak.smoothed
    times = [sample.time_seconds for sample in peak.samples]
    warnings: list[str] = []
    band = values[peak.peak] - PARAMETERS.peak_band_fraction * peak.rise
    start = peak.peak
    while start > 0 and values[start - 1] >= band:
        start -= 1
    end = peak.peak
    while end + 1 < len(values) and values[end + 1] >= band:
        end += 1
    confidence, components = _transition_confidence(peak, start, end)
    baseline = _percentile(values[: peak.peak], 0.20)
    start_level = baseline + 0.15 * (values[peak.peak] - baseline)
    if values[0] > start_level:
        backswing_start = 0
    else:
        last_low = 0
        for index in range(peak.peak):
            if values[index] <= start_level:
                last_low = index
        backswing_start = min(last_low + 1, start)
    backswing_end = start - 1
    if backswing_start == 0 and backswing_end >= backswing_start:
        warnings.append(
            "Backswing interval starts at the first tracked hand sample. "
            "The takeaway may be earlier than the pose track."
        )
        backswing_confidence = _cap(confidence * 0.85)
    else:
        backswing_confidence = confidence

    low = peak.peak
    index = peak.peak + 1
    while index < len(values):
        if values[index] <= values[low]:
            low = index
            index += 1
            continue
        if values[index] - values[low] >= PARAMETERS.min_follow_rise_torso:
            break
        index += 1

    stages: list[StageBoundary] = []
    if (
        backswing_start >= 3
        and times[backswing_start - 1] - times[0] >= PARAMETERS.setup_min_seconds
    ):
        prefix = values[:backswing_start]
        if max(prefix) - min(prefix) <= PARAMETERS.setup_max_range_torso:
            stages.append(
                _interval(
                    "SETUP",
                    peak.samples,
                    0,
                    backswing_start - 1,
                    confidence,
                    "pre_rise_hand_stability",
                )
            )
    if backswing_end >= backswing_start:
        stages.append(
            _interval(
                "BACKSWING",
                peak.samples,
                backswing_start,
                backswing_end,
                backswing_confidence,
                "wrist_elevation_rise",
            )
        )
    stages.append(
        _interval(
            "TRANSITION",
            peak.samples,
            start,
            end,
            confidence,
            "wrist_elevation_direction_change",
            components,
        )
    )
    if low > end:
        stages.append(
            _interval(
                "DOWNSWING",
                peak.samples,
                end + 1,
                low,
                confidence,
                "wrist_elevation_descent",
            )
        )
    follow_end = None
    if any(
        values[item] - values[low] >= PARAMETERS.min_follow_rise_torso
        for item in range(low + 1, len(values))
    ):
        follow_end = low
        for item in range(low + 1, len(values)):
            if values[item] >= values[follow_end]:
                follow_end = item
        if follow_end > low:
            stages.append(
                _interval(
                    "FOLLOW_THROUGH",
                    peak.samples,
                    low + 1,
                    follow_end,
                    confidence,
                    "wrist_elevation_recovery",
                )
            )
    if follow_end is not None and follow_end < len(values) - 1:
        tail = values[follow_end + 1 :]
        tail_times = times[follow_end + 1 :]
        tail_velocity = [
            item for item in peak.velocity[follow_end + 1 :] if item is not None
        ]
        duration = tail_times[-1] - times[follow_end]
        quiet = (
            duration >= PARAMETERS.finish_min_seconds
            and max(tail) - min(tail) <= PARAMETERS.finish_max_range_torso
            and (
                not tail_velocity
                or max(abs(item) for item in tail_velocity)
                <= PARAMETERS.finish_max_speed_torso_per_second
            )
        )
        if quiet:
            stages.append(
                _interval(
                    "FINISH",
                    peak.samples,
                    follow_end + 1,
                    len(values) - 1,
                    _cap(confidence * 0.9),
                    "trailing_low_hand_speed",
                )
            )
    order = {stage: position for position, stage in enumerate(TAXONOMY)}
    stages.sort(key=lambda item: (item.time_interval_seconds[0], order[item.stage]))
    return stages, warnings


def _excursion(peak: _Peak | None) -> HandExcursion:
    if peak is None:
        return HandExcursion(
            observed=False,
            rise_torso_lengths=None,
            fall_torso_lengths=None,
            peak_time_seconds=None,
            peak_tick=None,
            wrists_at_peak=None,
            gates_passed=False,
            reversal=False,
        )
    sample = peak.samples[peak.peak]
    return HandExcursion(
        observed=True,
        rise_torso_lengths=peak.rise,
        fall_torso_lengths=peak.fall,
        peak_time_seconds=sample.time_seconds,
        peak_tick=sample.tick,
        wrists_at_peak=sample.wrists_used,
        gates_passed=peak.gates_passed,
        reversal=peak.reversal,
    )


def _landmark_usable(frame: PoseFrame, name: str) -> bool:
    return _usable(frame.landmarks.get(name)) is not None


def _continuity(frames: Sequence[PoseFrame], name: str) -> LandmarkContinuity:
    usable = [
        index for index, frame in enumerate(frames) if _landmark_usable(frame, name)
    ]
    if not usable:
        return LandmarkContinuity(name, 0, 0, 0.0, 0)
    longest = 1
    run = 1
    gaps = 0
    for earlier, later in zip(usable, usable[1:]):
        elapsed = frames[later].timestamp_seconds - frames[earlier].timestamp_seconds
        if later == earlier + 1 and elapsed <= PARAMETERS.max_gap_seconds:
            run += 1
            longest = max(longest, run)
        else:
            gaps += 1
            run = 1
    return LandmarkContinuity(
        landmark=name,
        usable_frames=len(usable),
        longest_run=longest,
        continuity=longest / len(usable),
        gaps=gaps,
    )


def _missing(frames: Sequence[PoseFrame]) -> tuple[MissingInterval, ...]:
    intervals: list[MissingInterval] = []
    for name in LANDMARKS:
        start: int | None = None
        ticks: list[int] = []
        for index, frame in enumerate(frames):
            if _landmark_usable(frame, name):
                if start is not None:
                    intervals.append(
                        _missing_interval(frames, name, start, index - 1, ticks)
                    )
                    start = None
                    ticks = []
                continue
            if start is None:
                start = index
            ticks.append(frame.capture_tick)
        if start is not None:
            intervals.append(
                _missing_interval(frames, name, start, len(frames) - 1, ticks)
            )
    return tuple(intervals)


def _missing_interval(
    frames: Sequence[PoseFrame],
    name: str,
    start: int,
    end: int,
    ticks: list[int],
) -> MissingInterval:
    return MissingInterval(
        landmark=name,
        time_interval_seconds=(
            frames[start].timestamp_seconds,
            frames[end].timestamp_seconds,
        ),
        ticks=tuple(ticks),
        frame_count=len(ticks),
    )


def _tracking(frames: Sequence[PoseFrame]) -> TrackingSummary:
    count = len(frames)
    detected = sum(1 for frame in frames if frame.pose_detected)
    hands = sum(
        1
        for frame in frames
        if _landmark_usable(frame, "left_wrist")
        or _landmark_usable(frame, "right_wrist")
    )
    both = sum(
        1
        for frame in frames
        if _landmark_usable(frame, "left_wrist")
        and _landmark_usable(frame, "right_wrist")
    )
    return TrackingSummary(
        frame_count=count,
        pose_detected_frames=detected,
        pose_detection_rate=(detected / count) if count else 0.0,
        usable_hand_frames=hands,
        both_wrists_frames=both,
        continuity=tuple(_continuity(frames, name) for name in LANDMARKS),
        missing_landmark_intervals=_missing(frames),
    )


def load_camera_frames(
    shot_dir: Path, camera_id: str = "swing_behind"
) -> list[CameraFrame]:
    """Read one camera's diagnostic frames.

    The clock is `offset_seconds`, the screenshot request time. Strike time,
    replay-start time, and any selected phase are not read.
    """

    meta_path = shot_dir / "metadata.json"
    if not meta_path.is_file():
        raise ValueError(f"shot has no metadata.json: {shot_dir}")
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    timeline = payload.get("diagnostic_timeline")
    if not isinstance(timeline, dict):
        raise ValueError(f"shot has no diagnostic timeline: {shot_dir.name}")
    cameras = timeline.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError(f"diagnostic timeline has no cameras: {shot_dir.name}")
    raw_frames = None
    for camera in cameras:
        if isinstance(camera, dict) and camera.get("camera_id") == camera_id:
            frames = camera.get("frames")
            raw_frames = frames if isinstance(frames, list) else None
            break
    if not raw_frames:
        raise ValueError(f"diagnostic timeline has no {camera_id} frames")
    prefix = f"diagnostic/{camera_id}/"
    parsed: list[CameraFrame] = []
    for raw in raw_frames:
        if not isinstance(raw, dict):
            continue
        file = str(raw.get("file") or "").replace("\\", "/")
        tick = raw.get("capture_index")
        offset = raw.get("offset_seconds")
        if (
            not file.startswith(prefix)
            or ".." in Path(file).parts
            or isinstance(tick, bool)
            or not isinstance(tick, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int | float)
        ):
            continue
        path = (shot_dir / file).resolve()
        if not path.is_file() or shot_dir.resolve() not in path.parents:
            continue
        width = raw.get("width")
        height = raw.get("height")
        parsed.append(
            CameraFrame(
                capture_tick=tick,
                path=path,
                relative_file=file,
                timestamp_seconds=float(offset),
                width=width if isinstance(width, int) else 0,
                height=height if isinstance(height, int) else 0,
                sha256=str(raw.get("sha256") or ""),
            )
        )
    parsed.sort(
        key=lambda frame: (
            frame.timestamp_seconds,
            frame.capture_tick,
            frame.relative_file,
        )
    )
    if not parsed:
        raise ValueError(f"no readable {camera_id} frames under {shot_dir}")
    return parsed


def _load_rgb(path: Path) -> np.ndarray | None:
    from PIL import Image

    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            array = np.ascontiguousarray(rgb)
    except (OSError, ValueError):
        return None
    if array.ndim != 3 or array.shape[2] != 3:
        return None
    return array


def _empty_pose(frame: CameraFrame, width: int, height: int) -> PoseFrame:
    return PoseFrame(
        capture_tick=frame.capture_tick,
        timestamp_seconds=frame.timestamp_seconds,
        pose_detected=False,
        pose_presence=None,
        landmarks={},
        relative_file=frame.relative_file,
        image_path=str(frame.path),
        width=width,
        height=height,
        sha256=frame.sha256,
    )


def _pose_from_result(frame: CameraFrame, image: np.ndarray, result: Any) -> PoseFrame:
    height, width = image.shape[:2]
    poses = result.pose_landmarks or []
    if not poses:
        return _empty_pose(frame, width, height)
    landmarks: dict[str, LandmarkObservation] = {}
    raw = poses[0]
    presence_values: list[float] = []
    for name, index in _LANDMARK_INDEX.items():
        if index >= len(raw):
            continue
        point = raw[index]
        visibility = None if point.visibility is None else float(point.visibility)
        presence = None if point.presence is None else float(point.presence)
        if presence is not None:
            presence_values.append(presence)
        landmarks[name] = LandmarkObservation(
            x=float(point.x),
            y=float(point.y),
            visibility=visibility,
            presence=presence,
        )
    pose_presence = (
        sum(presence_values) / len(presence_values) if presence_values else None
    )
    return PoseFrame(
        capture_tick=frame.capture_tick,
        timestamp_seconds=frame.timestamp_seconds,
        pose_detected=True,
        pose_presence=pose_presence,
        landmarks=landmarks,
        relative_file=frame.relative_file,
        image_path=str(frame.path),
        width=width,
        height=height,
        sha256=frame.sha256,
    )


def open_pose_landmarker(model: Path) -> Any:
    """A new video-mode landmarker for one camera sequence.

    The caller closes it. Do not reuse it for another shot or another thread:
    video mode requires increasing timestamps, and the object is not shared.
    """

    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core import base_options as base_options_module

    options = vision.PoseLandmarkerOptions(
        base_options=base_options_module.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_segmentation_masks=False,
    )
    return vision.PoseLandmarker.create_from_options(options)


def track_poses(
    frames: Sequence[CameraFrame],
) -> tuple[list[PoseFrame], list[str], float, list[float]]:
    """Run the pose landmarker. Returns frames, warnings, init seconds, per-frame seconds."""

    from caddiej.swing_evidence.ready import ensure_evidence_ready

    ensure_evidence_ready()
    import mediapipe as mp

    model = ensure_pose_model()
    started = time.perf_counter()
    landmarker = open_pose_landmarker(model)
    init_seconds = time.perf_counter() - started
    poses: list[PoseFrame] = []
    inference: list[float] = []
    warnings: list[str] = []
    adjusted = False
    last_ms = -1
    try:
        for frame in frames:
            image = frame.rgb if frame.rgb is not None else _load_rgb(frame.path)
            if image is None:
                warnings.append(f"could not read {frame.relative_file}")
                continue
            timestamp_ms = int(round(frame.timestamp_seconds * 1000.0))
            if timestamp_ms <= last_ms:
                timestamp_ms = last_ms + 1
                adjusted = True
            last_ms = timestamp_ms
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image)
            t0 = time.perf_counter()
            try:
                result = landmarker.detect_for_video(mp_image, timestamp_ms)
            except RuntimeError as exc:
                inference.append(time.perf_counter() - t0)
                warnings.append(f"pose failed on tick {frame.capture_tick}: {exc}")
                poses.append(_empty_pose(frame, image.shape[1], image.shape[0]))
                continue
            inference.append(time.perf_counter() - t0)
            poses.append(_pose_from_result(frame, image, result))
    finally:
        landmarker.close()
    if adjusted:
        warnings.append(
            "Pose API timestamps were increased by 1 ms where capture "
            "timestamps were not strictly increasing. Stage times still use "
            "offset_seconds."
        )
    return poses, warnings, init_seconds, inference


def _working_set() -> tuple[int | None, int | None]:
    if os.name != "nt":
        return None, None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    windll = getattr(ctypes, "windll")
    get_process_memory_info = windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    ok = get_process_memory_info(
        windll.kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not ok:
        return None, None
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _pose_document(
    shot_id: str, camera_id: str, frames: Sequence[PoseFrame]
) -> dict[str, Any]:
    rows = []
    for frame in frames:
        landmarks = {}
        missing = []
        for name in LANDMARKS:
            point = frame.landmarks.get(name)
            if point is None:
                missing.append(name)
                continue
            used = _usable(point) is not None
            landmarks[name] = {
                "x": round(point.x, 6),
                "y": round(point.y, 6),
                "visibility": None
                if point.visibility is None
                else round(point.visibility, 6),
                "presence": None
                if point.presence is None
                else round(point.presence, 6),
                "used": used,
            }
            if not used:
                missing.append(name)
        rows.append(
            {
                "capture_tick": frame.capture_tick,
                "timestamp_seconds": round(frame.timestamp_seconds, 6),
                "relative_file": frame.relative_file,
                "width": frame.width,
                "height": frame.height,
                "sha256": frame.sha256,
                "pose_detected": frame.pose_detected,
                "pose_presence": None
                if frame.pose_presence is None
                else round(frame.pose_presence, 6),
                "landmarks": landmarks,
                "missing_landmarks": missing,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "shot_id": shot_id,
        "camera_id": camera_id,
        "coordinate_space": "normalized_image_xy_origin_top_left",
        "visibility_min": PARAMETERS.visibility_min,
        "pose_backend": POSE_BACKEND,
        "pose_model": POSE_MODEL_NAME,
        "tracked_landmarks": list(LANDMARKS),
        "frames": rows,
    }


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_overlay(image: Any, frame: PoseFrame) -> Any:
    """Draw used landmarks on a copy. The source image is not modified."""

    from PIL import ImageDraw

    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    for name, point in frame.landmarks.items():
        if name not in _COLORS or _usable(point) is None:
            continue
        x = min(width - 1, max(0, int(round(point.x * width))))
        y = min(height - 1, max(0, int(round(point.y * height))))
        radius = max(4, width // 80)
        color = _COLORS[name]
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline=color,
            width=max(2, radius // 4),
        )
    return canvas


def _open_overlay(frame: PoseFrame) -> Any | None:
    from PIL import Image

    if not frame.image_path:
        return None
    path = Path(frame.image_path)
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return render_overlay(image.convert("RGB"), frame)


def _section_indexes(
    frames: Sequence[PoseFrame], ticks: Sequence[int], pad: int = 2
) -> list[int]:
    indexes = [
        index for index, frame in enumerate(frames) if frame.capture_tick in set(ticks)
    ]
    if not indexes:
        return []
    start = max(0, min(indexes) - pad)
    end = min(len(frames) - 1, max(indexes) + pad)
    chosen = list(range(start, end + 1))
    if len(chosen) <= 8:
        return chosen
    thinned = []
    for step in range(8):
        position = round(step * (len(chosen) - 1) / 7)
        thinned.append(chosen[position])
    return sorted(set(thinned))


def _spread_indexes(frames: Sequence[PoseFrame]) -> list[int]:
    detected = [index for index, frame in enumerate(frames) if frame.pose_detected]
    pool = detected or list(range(len(frames)))
    if len(pool) <= 8:
        return pool
    thinned = []
    for step in range(8):
        position = round(step * (len(pool) - 1) / 7)
        thinned.append(pool[position])
    return sorted(set(thinned))


def write_review_sheet(path: Path, analysis: _Analysis) -> None:
    from PIL import Image, ImageDraw

    frames = analysis.frames
    sections: list[tuple[str, list[int]]] = []
    groups = [("accepted", item) for item in analysis.result.boundaries] + [
        ("candidate", item) for item in analysis.result.candidates
    ]
    for disposition, boundary in groups:
        indexes = _section_indexes(frames, boundary.evidence_ticks)
        start, end = boundary.time_interval_seconds
        title = (
            f"{disposition.upper()} {boundary.stage}  "
            f"{start:.3f}-{end:.3f}s  confidence {boundary.confidence:.2f}  "
            f"{boundary.method}"
        )
        sections.append((title, indexes))
    if not sections:
        sections.append(("no proposed boundary", _spread_indexes(frames)))
    thumb_height = 280
    label_height = 52
    margin = 12
    font = _font(16)
    small = _font(14)
    rows = []
    width = margin
    for title, indexes in sections:
        thumbs = []
        for index in indexes:
            frame = frames[index]
            overlay = _open_overlay(frame)
            if overlay is None:
                continue
            scale = thumb_height / overlay.height
            thumb = overlay.resize((max(1, int(overlay.width * scale)), thumb_height))
            thumbs.append((frame, thumb))
        if not thumbs:
            continue
        row_width = margin + sum(thumb.width + margin for _, thumb in thumbs)
        width = max(width, min(row_width, 1800))
        rows.append((title, thumbs))
    if not rows:
        image = Image.new("RGB", (640, 120), "white")
        ImageDraw.Draw(image).text(
            (16, 40), "no frames to review", fill="black", font=font
        )
        image.save(path, quality=85)
        return
    height = margin + sum(label_height + thumb_height + margin for _, thumbs in rows)
    sheet = Image.new("RGB", (max(width, 640), height), "white")
    draw = ImageDraw.Draw(sheet)
    y = margin
    for title, thumbs in rows:
        draw.text((margin, y), title, fill="black", font=font)
        y += label_height - 18
        x = margin
        for frame, thumb in thumbs:
            if x + thumb.width > sheet.width - margin:
                break
            sheet.paste(thumb, (x, y))
            label_top = y + thumb.height - 22
            draw.rectangle(
                (x, label_top, x + thumb.width, y + thumb.height), fill=(0, 0, 0)
            )
            draw.text(
                (x + 4, label_top + 2),
                f"t{frame.capture_tick} {frame.timestamp_seconds:.3f}s",
                fill="white",
                font=small,
            )
            x += thumb.width + margin
        y += thumb_height + margin
    sheet.save(path, quality=85)


def _map_x(value: float, start: float, end: float, left: int, right: int) -> int:
    span = end - start
    if span <= 0.0:
        return left
    fraction = min(1.0, max(0.0, (value - start) / span))
    return int(left + fraction * (right - left))


def write_trajectories(path: Path, analysis: _Analysis) -> None:
    from PIL import Image, ImageDraw

    width, height = 1200, 820
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(14)
    frames = analysis.frames
    if not frames:
        draw.text((24, 40), "no frames", fill="black", font=font)
        image.save(path)
        return
    t0 = frames[0].timestamp_seconds
    t1 = frames[-1].timestamp_seconds
    draw.text(
        (24, 8),
        "Normalized image y (up the image is up the plot). Used landmarks only.",
        fill="black",
        font=font,
    )
    _draw_series(
        draw,
        analysis,
        t0,
        t1,
        top=36,
        bottom=390,
        names=(
            "left_wrist",
            "right_wrist",
            "left_elbow",
            "right_elbow",
            "left_shoulder",
            "right_shoulder",
            "left_hip",
            "right_hip",
        ),
        value_of=lambda sample, name: sample.points.get(name, (None, None))[1],
        invert=True,
    )
    draw.text(
        (24, 408),
        "Hand elevation in torso lengths. Positive means the hands are above the hips.",
        fill="black",
        font=font,
    )
    _draw_elevation(draw, analysis, t0, t1, top=440, bottom=790)
    image.save(path)


def _bands(
    draw: Any,
    analysis: _Analysis,
    t0: float,
    t1: float,
    top: int,
    bottom: int,
    left: int,
    right: int,
) -> None:
    for boundary in analysis.result.candidates:
        _band(draw, boundary, t0, t1, top, bottom, left, right, outline=True)
    for boundary in analysis.result.boundaries:
        _band(draw, boundary, t0, t1, top, bottom, left, right, outline=False)


def _band(
    draw: Any,
    boundary: Any,
    t0: float,
    t1: float,
    top: int,
    bottom: int,
    left: int,
    right: int,
    *,
    outline: bool,
) -> None:
    color = _STAGE_COLORS.get(boundary.stage, (180, 180, 180))
    x0 = _map_x(boundary.time_interval_seconds[0], t0, t1, left, right)
    x1 = _map_x(boundary.time_interval_seconds[1], t0, t1, left, right)
    if x1 <= x0:
        x1 = x0 + 2
    if outline:
        draw.rectangle((x0, top, x1, bottom), outline=color)
    else:
        tint = tuple((channel + 255) // 2 for channel in color)
        draw.rectangle((x0, top, x1, bottom), fill=tint)


def _draw_series(
    draw: Any,
    analysis: _Analysis,
    t0: float,
    t1: float,
    top: int,
    bottom: int,
    names: Sequence[str],
    value_of: Callable[..., float | None],
    invert: bool,
) -> None:
    left, right = 50, 1170
    _bands(draw, analysis, t0, t1, top, bottom, left, right)
    draw.rectangle((left, top, right, bottom), outline=(80, 80, 80))
    for name in names:
        color = _COLORS[name]
        previous = None
        for sample in analysis.samples:
            value = value_of(sample, name)
            if value is None:
                previous = None
                continue
            clamped = min(1.0, max(0.0, value))
            x = _map_x(sample.time_seconds, t0, t1, left, right)
            # Image y grows downward. Plot it so the top of the frame is up.
            if invert:
                y = int(top + clamped * (bottom - top))
            else:
                y = int(bottom - clamped * (bottom - top))
            if previous is not None:
                draw.line((previous, (x, y)), fill=color, width=2)
            previous = (x, y)
    legend_x = 60
    for name in ("left_wrist", "right_wrist", "left_shoulder", "left_hip"):
        draw.rectangle((legend_x, top + 6, legend_x + 12, top + 18), fill=_COLORS[name])
        draw.text((legend_x + 16, top + 4), name, fill="black", font=_font(12))
        legend_x += 150


def _draw_elevation(
    draw: Any,
    analysis: _Analysis,
    t0: float,
    t1: float,
    top: int,
    bottom: int,
    left: int = 50,
    right: int = 1170,
) -> None:
    _bands(draw, analysis, t0, t1, top, bottom, left, right)
    draw.rectangle((left, top, right, bottom), outline=(80, 80, 80))
    values = [
        sample.elevation for sample in analysis.samples if sample.elevation is not None
    ]
    smoothed = list(analysis.smoothed_by_tick.values())
    pool = values + smoothed
    if not pool:
        draw.text(
            (left + 12, top + 12),
            "no usable hand elevation",
            fill="black",
            font=_font(14),
        )
        return
    low = min(pool) - 0.2
    high = max(pool) + 0.2
    span = high - low or 1.0

    def y_of(value: float) -> int:
        fraction = (value - low) / span
        return int(bottom - fraction * (bottom - top))

    for sample in analysis.samples:
        if sample.elevation is None:
            continue
        point = (
            _map_x(sample.time_seconds, t0, t1, left, right),
            y_of(sample.elevation),
        )
        radius = 3
        draw.ellipse(
            (
                point[0] - radius,
                point[1] - radius,
                point[0] + radius,
                point[1] + radius,
            ),
            fill=(40, 40, 40),
        )
    previous = None
    for sample in analysis.samples:
        value = analysis.smoothed_by_tick.get(sample.tick)
        if value is None:
            previous = None
            continue
        point = (_map_x(sample.time_seconds, t0, t1, left, right), y_of(value))
        if previous is not None:
            draw.line((previous, point), fill=(20, 80, 180), width=2)
        previous = point
    if analysis.result.hand_excursion.peak_time_seconds is not None:
        x = _map_x(
            analysis.result.hand_excursion.peak_time_seconds, t0, t1, left, right
        )
        draw.line((x, top, x, bottom), fill=(180, 40, 40), width=1)
        draw.text(
            (x + 4, top + 4), "hand elevation peak", fill=(180, 40, 40), font=_font(12)
        )


def write_timeline(path: Path, analysis: _Analysis) -> None:
    from PIL import Image, ImageDraw

    width, height = 1200, 420
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(14)
    frames = analysis.frames
    draw.text(
        (24, 10),
        f"{analysis.result.shot_id}  status={analysis.result.status}  "
        f"orientation={analysis.result.orientation}  clock={CLOCK}",
        fill="black",
        font=font,
    )
    if not frames:
        image.save(path)
        return
    t0 = frames[0].timestamp_seconds
    t1 = frames[-1].timestamp_seconds
    left, right = 180, 1160
    for row, stage in enumerate(TAXONOMY):
        y = 40 + row * 48
        draw.text((16, y + 8), stage, fill="black", font=font)
        draw.rectangle((left, y, right, y + 28), outline=(180, 180, 180))
        accepted = [item for item in analysis.result.boundaries if item.stage == stage]
        proposed = [item for item in analysis.result.candidates if item.stage == stage]
        if not accepted and not proposed:
            draw.text((left + 8, y + 6), "unresolved", fill=(120, 120, 120), font=font)
        for boundary in proposed:
            _timeline_bar(draw, boundary, t0, t1, left, right, y, candidate=True)
        for boundary in accepted:
            _timeline_bar(draw, boundary, t0, t1, left, right, y, candidate=False)
    image.save(path)


def _timeline_bar(
    draw: Any,
    boundary: Any,
    t0: float,
    t1: float,
    left: int,
    right: int,
    y: int,
    *,
    candidate: bool,
) -> None:
    color = _STAGE_COLORS.get(boundary.stage, (100, 100, 100))
    x0 = _map_x(boundary.time_interval_seconds[0], t0, t1, left, right)
    x1 = max(x0 + 3, _map_x(boundary.time_interval_seconds[1], t0, t1, left, right))
    if candidate:
        draw.rectangle((x0, y + 2, x1, y + 26), outline=color, width=2)
    else:
        draw.rectangle((x0, y + 2, x1, y + 26), fill=color)
    draw.text(
        (x0 + 4, y + 6),
        f"{boundary.confidence:.2f}",
        fill="black" if candidate else "white",
        font=_font(12),
    )


def _performance(
    *,
    shot_id: str,
    init_seconds: float | None,
    inference: list[float],
    total_seconds: float,
    decoded_frames: int,
) -> dict[str, Any]:
    current, peak = _working_set()
    mean = sum(inference) / len(inference) if inference else None
    return {
        "schema_version": SCHEMA_VERSION,
        "shot_id": shot_id,
        "pose_backend": POSE_BACKEND,
        "pose_model": POSE_MODEL_NAME,
        "model_init_seconds": None if init_seconds is None else round(init_seconds, 6),
        "decoded_frames": decoded_frames,
        "pose_inference_seconds": round(sum(inference), 6),
        "per_frame_inference_seconds": [round(item, 6) for item in inference],
        "per_frame_inference_seconds_mean": None if mean is None else round(mean, 6),
        "per_frame_inference_seconds_max": None
        if not inference
        else round(max(inference), 6),
        "total_seconds": round(total_seconds, 6),
        "process_working_set_bytes": current,
        "process_peak_working_set_bytes": peak,
        "notes": [
            "Working set is the whole Python process, not the model alone.",
            "Peak working set is the process lifetime peak and accumulates across shots in one run.",
            "Stage times use capture offset_seconds. Pose API milliseconds are only a tracker input.",
        ],
    }


def _stamp(result: SwingStageResult) -> SwingStageResult:
    created = datetime.now(UTC).isoformat()
    return replace(result, provenance=replace(result.provenance, created_at=created))


def run_shot(
    shot_dir: Path,
    results_dir: Path,
    camera_id: str = "swing_behind",
) -> tuple[SwingStageResult, dict[str, Any]]:
    """Track one shot and write its diagnostic artifacts. Original JPEGs are not modified."""

    shot_id = shot_dir.name
    out = results_dir / shot_id
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    init_seconds = None
    inference: list[float] = []
    decoded = 0
    try:
        camera_frames = load_camera_frames(shot_dir, camera_id)
        poses, pose_warnings, init_seconds, inference = track_poses(camera_frames)
        decoded = len(poses)
        analysis = _analyze(poses, shot_id=shot_id, camera_id=camera_id)
        result = replace(
            analysis.result,
            warnings=analysis.result.warnings
            + tuple(pose_warnings)
            + (_WARNING_JOINTS,),
        )
        analysis = replace(analysis, result=result)
    except (OSError, ValueError, RuntimeError) as exc:
        result = _failed(shot_id, camera_id, str(exc))
        total = time.perf_counter() - started
        performance = _performance(
            shot_id=shot_id,
            init_seconds=init_seconds,
            inference=inference,
            total_seconds=total,
            decoded_frames=decoded,
        )
        _write_json(out / "stage_boundaries.json", to_plain(_stamp(result)))
        _write_json(out / "pose_tracking.json", _pose_document(shot_id, camera_id, []))
        _write_json(out / "performance.json", performance)
        return result, performance
    _write_json(
        out / "pose_tracking.json", _pose_document(shot_id, camera_id, analysis.frames)
    )
    try:
        write_trajectories(out / "landmark_trajectories.png", analysis)
        write_timeline(out / "stage_timeline.png", analysis)
        write_review_sheet(out / "stage_review_sheet.jpg", analysis)
    except (OSError, ValueError) as exc:
        result = replace(
            analysis.result,
            warnings=analysis.result.warnings
            + (f"diagnostic images were not written: {exc}",),
        )
        analysis = replace(analysis, result=result)
    total = time.perf_counter() - started
    performance = _performance(
        shot_id=shot_id,
        init_seconds=init_seconds,
        inference=inference,
        total_seconds=total,
        decoded_frames=decoded,
    )
    _write_json(out / "stage_boundaries.json", to_plain(_stamp(analysis.result)))
    _write_json(out / "performance.json", performance)
    return analysis.result, performance


def _brief(result: SwingStageResult, performance: dict[str, Any]) -> dict[str, Any]:
    def pick(items: tuple[StageBoundary, ...], stage: str) -> dict[str, Any] | None:
        found = next((item for item in items if item.stage == stage), None)
        if found is None:
            return None
        return {
            "time_interval_seconds": list(found.time_interval_seconds),
            "confidence": found.confidence,
            "evidence_ticks": list(found.evidence_ticks),
            "method": found.method,
        }

    tracking = result.tracking
    wrist = None
    if tracking is not None:
        wrist = next(
            (item for item in tracking.continuity if item.landmark == "right_wrist"),
            None,
        )
        left = next(
            (item for item in tracking.continuity if item.landmark == "left_wrist"),
            None,
        )
    else:
        left = None
    return {
        "shot_id": result.shot_id,
        "status": result.status,
        "orientation": result.orientation,
        "pose_detection_rate": None
        if tracking is None
        else tracking.pose_detection_rate,
        "frame_count": None if tracking is None else tracking.frame_count,
        "usable_hand_frames": None if tracking is None else tracking.usable_hand_frames,
        "both_wrists_frames": None if tracking is None else tracking.both_wrists_frames,
        "left_wrist_continuity": None if left is None else left.continuity,
        "right_wrist_continuity": None if wrist is None else wrist.continuity,
        "transition": pick(result.boundaries, "TRANSITION")
        or pick(result.candidates, "TRANSITION"),
        "transition_disposition": (
            "accepted"
            if pick(result.boundaries, "TRANSITION")
            else "candidate"
            if pick(result.candidates, "TRANSITION")
            else "none"
        ),
        "downswing": pick(result.boundaries, "DOWNSWING")
        or pick(result.candidates, "DOWNSWING"),
        "downswing_disposition": (
            "accepted"
            if pick(result.boundaries, "DOWNSWING")
            else "candidate"
            if pick(result.candidates, "DOWNSWING")
            else "none"
        ),
        "impact_supported": result.impact.supported,
        "unresolved_stages": list(result.unresolved_stages),
        "hand_excursion": to_plain(result.hand_excursion),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
        "performance": {
            "model_init_seconds": performance.get("model_init_seconds"),
            "per_frame_inference_seconds_mean": performance.get(
                "per_frame_inference_seconds_mean"
            ),
            "per_frame_inference_seconds_max": performance.get(
                "per_frame_inference_seconds_max"
            ),
            "pose_inference_seconds": performance.get("pose_inference_seconds"),
            "total_seconds": performance.get("total_seconds"),
            "process_working_set_bytes": performance.get("process_working_set_bytes"),
            "process_peak_working_set_bytes": performance.get(
                "process_peak_working_set_bytes"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect swing stages for archived shots."
    )
    parser.add_argument("shot_id", nargs="+")
    parser.add_argument("--shots-root", type=Path, default=DEFAULT_SHOTS)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--camera", default="swing_behind")
    args = parser.parse_args(argv)
    args.results_root.mkdir(parents=True, exist_ok=True)
    briefs = []
    failed = False
    for shot_id in args.shot_id:
        shot_dir = args.shots_root / shot_id
        result, performance = run_shot(shot_dir, args.results_root, args.camera)
        brief = _brief(result, performance)
        briefs.append(brief)
        transition = brief["transition"]
        interval = None if transition is None else transition["time_interval_seconds"]
        rate = brief["pose_detection_rate"]
        rate_text = "n/a" if rate is None else f"{rate:.2f}"
        print(
            f"{shot_id} status={result.status} pose={rate_text} "
            f"transition={brief['transition_disposition']} {interval} "
            f"downswing={brief['downswing_disposition']} "
            f"impact_supported={result.impact.supported}"
        )
        if result.status == STATUS_FAILED:
            failed = True
    _write_json(
        args.results_root / "summary.json",
        {
            "schema_version": SCHEMA_VERSION,
            "detector_version": DETECTOR_VERSION,
            "camera_id": args.camera,
            "confidence_scale": CONFIDENCE_SCALE,
            "shots": briefs,
        },
    )
    print(args.results_root)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
