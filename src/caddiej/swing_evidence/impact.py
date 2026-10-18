"""Estimate the interval where a stationary ball leaves, from the frames themselves.

The result is an interval between two original frames. It is not a claim that
a club was seen touching the ball, and it is not an optical-flow peak. A
compact spot has to sit still and then change while its surroundings do not.
Anything else stays unresolved.

ProTee's shot event, replay-start offsets, phase aims, and hand-labeled
contact ticks are not inputs. The clock is the timestamp on each frame.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\impact_estimator.py
is not a CLI. The evidence package runner calls this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from caddiej.swing_evidence.contracts import (
    CONFIDENCE_SCALE,
    IMPACT_ESTIMATED,
    IMPACT_UNRESOLVED,
    OBSERVED_BALL_MOTION,
    SCHEMA_VERSION,
    UNSUPPORTED_ESTIMATE,
    ImpactEstimate,
    Provenance,
    RejectedImpactCandidate,
)
from caddiej.swing_evidence.workers import map_ordered

PRODUCER = "impact_estimator"
PRODUCER_VERSION = "1"
METHOD = "local_ball_appearance_change"

# Shared by every shot and both cameras. Distances are pixels on the analysis
# grid. Gray levels are 0-255. Nothing here is a probability.
ANALYSIS_WIDTH = 480
GROUND_TOP = 0.40
GROUND_BOTTOM = 0.97
REFERENCE_FRAMES = 4
MIN_STABLE_SECONDS = 0.20
PERSIST_FRAMES = 2
PERSIST_FRACTION = 0.70
MIN_AREA = 4
MAX_AREA = 200
MIN_ASPECT = 0.55
MIN_FILL = 0.40
ONSET_GRAY = 20.0
STABLE_GRAY = 12.0
RING_RATIO = 0.70
MIN_CONTRAST = 12.0
DISAGREEMENT_PERIODS = 2.0


@dataclass(frozen=True, slots=True)
class _Blob:
    x: int
    y: int
    radius: int
    area: int


def _parameters() -> tuple[tuple[str, float | int], ...]:
    return (
        ("analysis_width", ANALYSIS_WIDTH),
        ("ground_top", GROUND_TOP),
        ("ground_bottom", GROUND_BOTTOM),
        ("reference_frames", REFERENCE_FRAMES),
        ("min_stable_seconds", MIN_STABLE_SECONDS),
        ("persist_frames", PERSIST_FRAMES),
        ("persist_fraction", PERSIST_FRACTION),
        ("min_area", MIN_AREA),
        ("max_area", MAX_AREA),
        ("min_aspect", MIN_ASPECT),
        ("min_fill", MIN_FILL),
        ("onset_gray", ONSET_GRAY),
        ("stable_gray", STABLE_GRAY),
        ("ring_ratio", RING_RATIO),
        ("min_contrast", MIN_CONTRAST),
        ("disagreement_periods", DISAGREEMENT_PERIODS),
    )


def _provenance(camera_id: str | None) -> Provenance:
    stage = "impact_estimation" if camera_id else "impact_estimation_fused"
    return Provenance(
        stage=stage,
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
    )


def _unresolved(
    shot_id: str,
    camera_id: str | None,
    reason: str,
    *,
    rejected: Sequence[RejectedImpactCandidate] = (),
    warnings: Sequence[str] = (),
) -> ImpactEstimate:
    return ImpactEstimate(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=IMPACT_UNRESOLVED,
        provenance=_provenance(camera_id),
        classification=UNSUPPORTED_ESTIMATE,
        reason=reason,
        confidence=None,
        confidence_scale=CONFIDENCE_SCALE,
        method=METHOD,
        camera_id=camera_id,
        contact_interval_ticks=None,
        contact_interval_seconds=None,
        rejected_candidates=tuple(rejected),
        parameters=_parameters(),
        warnings=tuple(warnings),
    )


def _scale(gray: np.ndarray) -> np.ndarray:
    height, width = gray.shape[:2]
    if width <= ANALYSIS_WIDTH:
        return gray
    new_height = max(1, int(round(height * (ANALYSIS_WIDTH / width))))
    return cv2.resize(gray, (ANALYSIS_WIDTH, new_height), interpolation=cv2.INTER_AREA)


def _band(height: int) -> tuple[int, int]:
    top = int(round(height * GROUND_TOP))
    bottom = int(round(height * GROUND_BOTTOM))
    return top, max(top + 1, bottom)


def _blobs(median: np.ndarray) -> list[_Blob]:
    height, width = median.shape
    top, bottom = _band(height)
    view = median[top:bottom]
    if view.size == 0:
        return []
    sigma = max(3.0, width * 0.015)
    blurred = cv2.GaussianBlur(view, (0, 0), sigmaX=sigma, sigmaY=sigma)
    high = view.astype(np.float32) - blurred.astype(np.float32)
    floor = float(np.percentile(high, 99.2))
    level = max(MIN_CONTRAST, floor)
    mask = (high >= level).astype(np.uint8)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)  # type: ignore[call-overload]
    found: list[_Blob] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        box_w = int(stats[label, cv2.CC_STAT_WIDTH])
        box_h = int(stats[label, cv2.CC_STAT_HEIGHT])
        if area < MIN_AREA or area > MAX_AREA or box_w < 1 or box_h < 1:
            continue
        aspect = min(box_w, box_h) / max(box_w, box_h)
        fill = area / float(box_w * box_h)
        if aspect < MIN_ASPECT or fill < MIN_FILL:
            continue
        center_x = int(round(float(centroids[label][0])))
        center_y = int(round(float(centroids[label][1]))) + top
        if not (1 <= center_x < width - 1 and top + 1 <= center_y < bottom - 1):
            continue
        radius = max(1, int(round(0.5 * max(box_w, box_h))))
        found.append(_Blob(center_x, center_y, radius, area))
    return found


def _window(shape: tuple[int, int], blob: _Blob, pad: int) -> tuple[int, int, int, int]:
    height, width = shape
    radius = blob.radius + pad
    x0 = max(0, blob.x - radius)
    x1 = min(width, blob.x + radius + 1)
    y0 = max(0, blob.y - radius)
    y1 = min(height, blob.y + radius + 1)
    return x0, y0, x1, y1


def _core_and_ring(
    base: np.ndarray, frame: np.ndarray, blob: _Blob
) -> tuple[float, float]:
    x0, y0, x1, y1 = _window(base.shape, blob, 0)
    core_base = base[y0:y1, x0:x1].astype(np.float32)
    core = frame[y0:y1, x0:x1].astype(np.float32)
    if core.size == 0:
        return 0.0, 0.0
    core_change = float(np.mean(np.abs(core_base - core)))
    rx0, ry0, rx1, ry1 = _window(base.shape, blob, max(2, blob.radius))
    ring_base = base[ry0:ry1, rx0:rx1].astype(np.float32)
    ring = frame[ry0:ry1, rx0:rx1].astype(np.float32)
    ring_change = float(np.mean(np.abs(ring_base - ring)))
    return core_change, ring_change


def _local(core_change: float, ring_change: float) -> bool:
    return core_change >= ONSET_GRAY and ring_change < RING_RATIO * core_change


def estimate_ball_motion(
    ticks: Sequence[int],
    times: Sequence[float],
    grays: Sequence[np.ndarray],
    *,
    shot_id: str,
    camera_id: str,
    useful_start: int = 0,
) -> ImpactEstimate:
    """Look for one stationary compact spot that then changes and stays changed."""

    if not shot_id:
        raise ValueError("shot_id is required")
    count = len(grays)
    if not (count == len(ticks) == len(times)):
        raise ValueError("ticks, times, and frames differ in length")
    start = min(max(0, useful_start), count)
    usable = count - start
    if usable < REFERENCE_FRAMES + PERSIST_FRAMES:
        return _unresolved(shot_id, camera_id, "insufficient_frames")
    scaled = map_ordered(_scale, grays)
    reference = scaled[start : start + REFERENCE_FRAMES]
    median = np.median(np.stack(reference, axis=0), axis=0).astype(np.uint8)
    blobs = _blobs(median)
    if not blobs:
        return _unresolved(shot_id, camera_id, "ball_not_visible")

    rejected: list[RejectedImpactCandidate] = []
    accepted: list[tuple[_Blob, int, float]] = []
    span = float(times[start + REFERENCE_FRAMES - 1]) - float(times[start])
    for blob in blobs:
        series = [_core_and_ring(median, frame, blob) for frame in scaled]
        prefix = series[start : start + REFERENCE_FRAMES]
        if (
            any(core > STABLE_GRAY for core, _ring in prefix)
            or span < MIN_STABLE_SECONDS
        ):
            rejected.append(
                _rejected(blob, scaled[0].shape, "unstable_before_motion", None)
            )
            continue
        onset = _onset_index(series, start + REFERENCE_FRAMES)
        if onset is None:
            rejected.append(_rejected(blob, scaled[0].shape, "no_motion_onset", None))
            continue
        held = series[onset:]
        if not held or sum(
            1 for core, ring in held if _local(core, ring)
        ) < PERSIST_FRACTION * len(held):
            rejected.append(
                _rejected(blob, scaled[0].shape, "change_did_not_persist", ticks[onset])
            )
            continue
        accepted.append((blob, onset, series[onset][0]))

    if not accepted:
        reason = rejected[-1].reason if rejected else "ball_not_visible"
        if any(item.reason == "no_motion_onset" for item in rejected) and not any(
            item.reason == "change_did_not_persist" for item in rejected
        ):
            reason = "no_motion_onset"
        return _unresolved(shot_id, camera_id, reason, rejected=rejected)

    blob, onset, strength = max(accepted, key=lambda item: (item[2], -item[1]))
    for other, other_onset, _strength in accepted:
        if other is blob:
            continue
        rejected.append(
            _rejected(
                other, scaled[0].shape, "weaker_than_accepted_blob", ticks[other_onset]
            )
        )
    still = onset - 1
    height, width = scaled[0].shape
    support = [ticks[still], ticks[onset]]
    if onset + 1 < count:
        support.append(ticks[onset + 1])
    confidence = _confidence(span, True)
    return ImpactEstimate(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=IMPACT_ESTIMATED,
        provenance=_provenance(camera_id),
        classification=OBSERVED_BALL_MOTION,
        reason="stationary_spot_changed_and_stayed_changed",
        confidence=confidence,
        confidence_scale=CONFIDENCE_SCALE,
        method=METHOD,
        camera_id=camera_id,
        contact_interval_ticks=(ticks[still], ticks[onset]),
        contact_interval_seconds=(float(times[still]), float(times[onset])),
        supporting_ticks=tuple(support),
        normalized_ball=(blob.x / width, blob.y / height),
        rejected_candidates=tuple(rejected),
        parameters=_parameters(),
        warnings=(
            "The interval is when a stationary spot left. "
            "The club meeting the ball was not observed, so this is not "
            "an inferred contact claim.",
        ),
    )


def _onset_index(series: Sequence[tuple[float, float]], begin: int) -> int | None:
    limit = len(series) - PERSIST_FRAMES + 1
    for index in range(begin, max(begin, limit)):
        window = series[index : index + PERSIST_FRAMES]
        if len(window) < PERSIST_FRAMES:
            return None
        if all(_local(core, ring) for core, ring in window):
            return index
    return None


def _rejected(
    blob: _Blob, shape: tuple[int, int], reason: str, onset_tick: int | None
) -> RejectedImpactCandidate:
    height, width = shape
    return RejectedImpactCandidate(
        normalized_x=blob.x / width,
        normalized_y=blob.y / height,
        reason=reason,
        onset_tick=onset_tick,
    )


def _confidence(stable_seconds: float, persisted: bool) -> float:
    score = 0.55
    if stable_seconds >= 0.40:
        score += 0.10
    if persisted:
        score += 0.10
    return min(0.90, score)


def _period(times: Sequence[float]) -> float:
    deltas = [
        float(right) - float(left)
        for left, right in zip(times, times[1:], strict=False)
        if float(right) > float(left)
    ]
    if not deltas:
        return 0.10
    deltas.sort()
    return deltas[len(deltas) // 2]


def fuse_impact(
    estimates: Sequence[ImpactEstimate],
    periods: Sequence[float],
) -> ImpactEstimate:
    """Keep one camera when the other saw nothing. Refuse a pair that disagrees."""

    if not estimates:
        raise ValueError("at least one camera estimate is required")
    estimated = [item for item in estimates if item.status == IMPACT_ESTIMATED]
    if not estimated:
        reasons = [item.reason for item in estimates]
        if reasons and all(reason == "ball_not_visible" for reason in reasons):
            reason = "ball_not_visible"
        else:
            specific = [reason for reason in reasons if reason != "ball_not_visible"]
            reason = specific[0] if specific else (reasons[0] if reasons else "ball_not_visible")
        return _unresolved(
            estimates[0].shot_id,
            None,
            reason,
            warnings=tuple(
                f"{item.camera_id}: {item.reason}"
                for item in estimates
                if item.camera_id
            ),
        )
    if len(estimated) == 1:
        only = estimated[0]
        missing = [item for item in estimates if item is not only]
        warnings = list(only.warnings)
        for item in missing:
            warnings.append(f"{item.camera_id or 'camera'} unresolved: {item.reason}")
        return ImpactEstimate(
            schema_version=only.schema_version,
            shot_id=only.shot_id,
            status=only.status,
            provenance=_provenance(None),
            classification=only.classification,
            reason=only.reason,
            confidence=only.confidence,
            confidence_scale=only.confidence_scale,
            method=only.method,
            camera_id=only.camera_id,
            contact_interval_ticks=only.contact_interval_ticks,
            contact_interval_seconds=only.contact_interval_seconds,
            supporting_ticks=only.supporting_ticks,
            normalized_ball=only.normalized_ball,
            rejected_candidates=only.rejected_candidates,
            camera_estimates=tuple(estimates),
            parameters=only.parameters,
            warnings=tuple(warnings),
        )
    return _fuse_pair(estimated[0], estimated[1], periods)


def _fuse_pair(
    left: ImpactEstimate,
    right: ImpactEstimate,
    periods: Sequence[float],
) -> ImpactEstimate:
    left_span = left.contact_interval_seconds or (0.0, 0.0)
    right_span = right.contact_interval_seconds or (0.0, 0.0)
    gap = max(0.0, max(left_span[0], right_span[0]) - min(left_span[1], right_span[1]))
    period = max([*periods, 0.05])
    children = (left, right)
    if gap > DISAGREEMENT_PERIODS * period:
        return ImpactEstimate(
            schema_version=SCHEMA_VERSION,
            shot_id=left.shot_id,
            status=IMPACT_UNRESOLVED,
            provenance=_provenance(None),
            classification=UNSUPPORTED_ESTIMATE,
            reason="camera_disagreement",
            confidence=None,
            confidence_scale=CONFIDENCE_SCALE,
            method=METHOD,
            camera_id=None,
            contact_interval_ticks=None,
            contact_interval_seconds=None,
            camera_estimates=children,
            parameters=_parameters(),
            warnings=(
                "The two cameras placed ball departure in different intervals, "
                "so neither is used for frame selection.",
            ),
        )
    start = (
        min(left_span[0], right_span[0]) if gap else max(left_span[0], right_span[0])
    )
    end = max(left_span[1], right_span[1]) if gap else min(left_span[1], right_span[1])
    confidence = min(
        0.90,
        ((left.confidence or 0.0) + (right.confidence or 0.0)) / 2.0
        + (0.0 if gap else 0.08),
    )
    warning = (
        "Cameras agree on an overlapping bracket."
        if gap == 0.0
        else "Cameras are within two frame periods. The reported interval is their union."
    )
    return ImpactEstimate(
        schema_version=SCHEMA_VERSION,
        shot_id=left.shot_id,
        status=IMPACT_ESTIMATED,
        provenance=_provenance(None),
        classification=OBSERVED_BALL_MOTION,
        reason="two_cameras_ball_motion_onset",
        confidence=confidence,
        confidence_scale=CONFIDENCE_SCALE,
        method=METHOD,
        camera_id=None,
        contact_interval_ticks=None,
        contact_interval_seconds=(start, end),
        supporting_ticks=tuple(
            dict.fromkeys((*left.supporting_ticks, *right.supporting_ticks))
        ),
        camera_estimates=children,
        parameters=_parameters(),
        warnings=(*left.warnings, *right.warnings, warning),
    )
