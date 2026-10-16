"""Deterministic visual temporal sampler. Research only.

A chronological camera sequence is reduced to a small set of the original
full-resolution frames. Frame difference and dense optical flow decide where
the sequence changes quickly. Those scores are rates: displacement divided by
the real elapsed time between the two frames.

The sampler does not name swing positions, does not read labels, and does not
call a model. A later request can attach the selected files one by one.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\temporal_sampler.py <shot-id>
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

CAPTURES = Path(__file__).resolve().parents[2] / "media" / "swing_shots"
RESULTS = Path(__file__).resolve().parent / "results" / "temporal_sampler"

# Same analysis grid for every sequence. Wider frames are reduced; narrower
# ones are left alone so a small synthetic image is not invented into detail.
ANALYSIS_WIDTH = 160
BLUR_KSIZE = 5
UPPER_QUANTILE = 0.75
# A pair that replaces most of the picture and is an outlier for its own
# sequence is a cut, not local motion. The strongest such cut begins the
# useful sequence. Frames before the first settled frame after that cut are
# left out of the budget.
SCENE_CHANGE_FRACTION = 0.5
PIXEL_CHANGE_FLOOR = 6.0
BASELINE_FRACTION = 0.5
ROI_ACTIVITY_PERCENTILE = 75.0
ROI_MIN_AREA_FRACTION = 0.30
ROI_MAX_AREA_FRACTION = 0.92
ROI_PAD_FRACTION = 0.08
ROI_MIN_SIDE = 32
HIGH_MOTION_Z = 1.0
BORDER_MARGIN = 0.08
DEFAULT_BUDGET = 24
_MAD_SCALE = 1.4826
_FARNEBACK = (0.5, 3, 15, 3, 5, 1.2, 0)

_REASONS = frozenset(
    {"endpoint", "baseline", "motion", "gap_fill", "all_available", "sequence_start"}
)


@dataclass(frozen=True)
class TimelineFrame:
    """One original frame and the timestamps stored beside it."""

    capture_tick: int
    path: Path
    relative_file: str
    offset_seconds: float
    replay_seconds: float | None
    requested_at: str | None
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class PairMeasurement:
    """Motion across the interval that ends on a frame."""

    delta_seconds: float
    frame_difference_score: float
    frame_difference_rate: float
    optical_flow_displacement: float
    optical_flow_rate: float
    changed_fraction: float
    nonpositive_dt: bool


@dataclass(frozen=True)
class Chosen:
    index: int
    reason: str


@dataclass(frozen=True)
class ActivityBox:
    """Inclusive-exclusive box on the analysis grid, plus the decision."""

    accepted: bool
    reason: str
    x: int
    y: int
    width: int
    height: int
    area_fraction: float


def load_timeline(
    shot_dir: Path, camera_id: str = "swing_behind"
) -> list[TimelineFrame]:
    """Read one camera from a diagnostic timeline.

    Replay timing is kept so a person can inspect it. Selection uses
    `offset_seconds`, which is when the screenshot was requested.
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
    raw_frames: list[Any] | None = None
    for camera in cameras:
        if isinstance(camera, dict) and camera.get("camera_id") == camera_id:
            frames = camera.get("frames")
            raw_frames = frames if isinstance(frames, list) else None
            break
    if not raw_frames:
        raise ValueError(f"diagnostic timeline has no {camera_id} frames")
    return _parse_frames(shot_dir, camera_id, raw_frames)


def _parse_frames(
    shot_dir: Path, camera_id: str, raw_frames: Sequence[Any]
) -> list[TimelineFrame]:
    prefix = f"diagnostic/{camera_id}/"
    parsed: list[TimelineFrame] = []
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
        replay = raw.get("offset_from_replay_start_seconds")
        requested = raw.get("requested_at")
        width = raw.get("width")
        height = raw.get("height")
        parsed.append(
            TimelineFrame(
                capture_tick=tick,
                path=path,
                relative_file=file,
                offset_seconds=float(offset),
                replay_seconds=float(replay)
                if isinstance(replay, int | float)
                else None,
                requested_at=requested if isinstance(requested, str) else None,
                width=width if isinstance(width, int) else 0,
                height=height if isinstance(height, int) else 0,
                sha256=str(raw.get("sha256") or ""),
            )
        )
    parsed.sort(
        key=lambda frame: (
            frame.offset_seconds,
            frame.capture_tick,
            frame.relative_file,
        )
    )
    if not parsed:
        raise ValueError(f"no readable {camera_id} frames under {shot_dir}")
    return parsed


def to_analysis(gray: np.ndarray) -> np.ndarray:
    """Grayscale image on the shared analysis grid, lightly blurred."""

    if gray.ndim != 2:
        raise ValueError("analysis expects a grayscale image")
    height, width = gray.shape
    if width > ANALYSIS_WIDTH:
        new_height = max(1, int(round(height * (ANALYSIS_WIDTH / width))))
        gray = cv2.resize(
            gray, (ANALYSIS_WIDTH, new_height), interpolation=cv2.INTER_AREA
        )
    if BLUR_KSIZE > 1:
        gray = cv2.GaussianBlur(gray, (BLUR_KSIZE, BLUR_KSIZE), 0)
    return np.ascontiguousarray(gray)


def _upper_mean(values: np.ndarray, quantile: float = UPPER_QUANTILE) -> float:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    if flat.size == 0:
        return 0.0
    thresh = float(np.quantile(flat, quantile))
    kept = flat[flat >= thresh]
    if kept.size == 0:
        return thresh
    return float(kept.mean())


def measure_pair(
    previous: np.ndarray, current: np.ndarray, dt: float
) -> PairMeasurement:
    """Frame-difference and Farneback rates between two analysis frames."""

    before = to_analysis(previous)
    after = to_analysis(current)
    if before.shape != after.shape:
        after = cv2.resize(
            after, (before.shape[1], before.shape[0]), interpolation=cv2.INTER_AREA
        )
    return _measure_ready(before, after, dt, PIXEL_CHANGE_FLOOR)


def _measure_ready(
    before: np.ndarray, after: np.ndarray, dt: float, change_threshold: float
) -> PairMeasurement:
    diff = cv2.absdiff(before, after)
    flow = cv2.calcOpticalFlowFarneback(before, after, None, *_FARNEBACK)
    magnitude = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    return _pack(diff, magnitude, dt, change_threshold)


def _prepare_all(grays: Sequence[np.ndarray]) -> list[np.ndarray]:
    prepared = [to_analysis(gray) for gray in grays]
    shape = prepared[0].shape
    fitted: list[np.ndarray] = []
    for gray in prepared:
        if gray.shape != shape:
            gray = cv2.resize(gray, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        fitted.append(np.ascontiguousarray(gray))
    return fitted


def measure_series(
    frames: Sequence[np.ndarray], times: Sequence[float]
) -> list[PairMeasurement]:
    """Pair measurements for a series. `times` are real seconds."""

    if len(frames) != len(times):
        raise ValueError("frames and timestamps differ in length")
    if len(frames) < 2:
        return []
    measured, _magnitudes, _diff_s, _flow_s = _score_prepared(
        _prepare_all(frames), times
    )
    return measured


def _pack(
    diff: np.ndarray, magnitude: np.ndarray, dt: float, change_threshold: float
) -> PairMeasurement:
    score = _upper_mean(diff)
    displacement = _upper_mean(magnitude)
    nonpositive = dt <= 1e-6
    return PairMeasurement(
        delta_seconds=float(dt),
        frame_difference_score=score,
        frame_difference_rate=0.0 if nonpositive else score / dt,
        optical_flow_displacement=displacement,
        optical_flow_rate=0.0 if nonpositive else displacement / dt,
        changed_fraction=float(np.mean(diff >= change_threshold)),
        nonpositive_dt=nonpositive,
    )


def _score_prepared(
    prepared: Sequence[np.ndarray], times: Sequence[float]
) -> tuple[list[PairMeasurement], list[np.ndarray], float, float]:
    """Score a series once. Returns measurements, flow magnitudes, and the two costs."""

    diff_started = time.perf_counter()
    diffs: list[np.ndarray] = []
    for index in range(1, len(prepared)):
        diffs.append(cv2.absdiff(prepared[index - 1], prepared[index]))
    diff_seconds = time.perf_counter() - diff_started
    flow_started = time.perf_counter()
    magnitudes: list[np.ndarray] = []
    for index in range(1, len(prepared)):
        flow = cv2.calcOpticalFlowFarneback(
            prepared[index - 1], prepared[index], None, *_FARNEBACK
        )
        magnitudes.append(np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2))
    flow_seconds = time.perf_counter() - flow_started
    medians = [float(np.median(diff)) for diff in diffs]
    noise = (
        float(np.percentile(np.asarray(medians, dtype=np.float64), 25))
        if medians
        else 0.0
    )
    threshold = max(PIXEL_CHANGE_FLOOR, noise * 4.0)
    measured = [
        _pack(diff, magnitude, float(times[index + 1]) - float(times[index]), threshold)
        for index, (diff, magnitude) in enumerate(zip(diffs, magnitudes, strict=True))
    ]
    return measured, magnitudes, diff_seconds, flow_seconds


def scene_change_mask(measurements: Sequence[PairMeasurement]) -> list[bool]:
    """True where a pair replaces most of the picture and is unusual here."""

    if not measurements:
        return []
    fractions = np.asarray(
        [item.changed_fraction for item in measurements], dtype=np.float64
    )
    median = float(np.median(fractions))
    limit = max(SCENE_CHANGE_FRACTION, float(np.percentile(fractions, 90)))
    outlier = max(SCENE_CHANGE_FRACTION, median * 3.0)
    return [
        float(fraction) >= limit and float(fraction) >= outlier
        for fraction in fractions
    ]


def useful_sequence_bounds(
    scene_changes: Sequence[bool], measurements: Sequence[PairMeasurement]
) -> tuple[int, int | None]:
    """First settled frame after the strongest full-frame cut.

    Pair ``i`` is the change that arrives on frame ``i + 1``. Among pairs the
    existing cut rule already flagged, the largest changed fraction is the
    boundary. The arrival frame is unsettled when the pair that leaves it is
    also a cut; the next frame is then tried. With no cut, the sequence starts
    at the first frame.
    """

    flagged = [index for index, flag in enumerate(scene_changes) if flag]
    if not flagged or not measurements:
        return 0, None
    cut = max(flagged, key=lambda index: (measurements[index].changed_fraction, -index))
    frame_count = len(measurements) + 1
    start = min(cut + 1, frame_count - 1)
    while (
        start < frame_count - 1 and start < len(scene_changes) and scene_changes[start]
    ):
        start += 1
    return start, cut


def _select_on_useful_sequence(
    times: Sequence[float],
    used: Sequence[PairMeasurement],
    scene: Sequence[bool],
    full: Sequence[PairMeasurement],
    identities: Sequence[str],
    budget: int,
) -> tuple[list[Chosen], list[float], int, int | None]:
    """Run the existing sampler on the frames at and after the settled start."""

    start, cut = useful_sequence_bounds(scene, full)
    local_times = list(times[start:])
    local_ids = list(identities[start:])
    local_used = list(used[start:])
    local_scene = list(scene[start:])
    if not local_times:
        return [], [], start, cut
    if not local_used:
        reason = "sequence_start" if cut is not None else "all_available"
        return [Chosen(start, reason)], [], start, cut
    combined = combined_motion(local_used, local_scene)[2]
    chosen_local = select_frames(
        local_times,
        frame_weights(combined),
        budget=budget,
        identities=local_ids,
    )
    chosen: list[Chosen] = []
    for item in chosen_local:
        reason = item.reason
        if cut is not None and item.index == 0:
            reason = "sequence_start"
        chosen.append(Chosen(item.index + start, reason))
    return chosen, combined, start, cut


def robust_z(values: Sequence[float]) -> list[float]:
    """Median/MAD z-scores. A flat series is all zero, not a false peak."""

    data = np.asarray(list(values), dtype=np.float64)
    if data.size == 0:
        return []
    center = float(np.median(data))
    mad = float(np.median(np.abs(data - center)))
    scale = _MAD_SCALE * mad
    if scale < 1e-9:
        return [0.0] * int(data.size)
    return ((data - center) / scale).tolist()


def combined_motion(
    measurements: Sequence[PairMeasurement], scene_changes: Sequence[bool]
) -> tuple[list[float], list[float], list[float]]:
    """Average of the two clipped robust rates. Cuts contribute nothing."""

    if len(scene_changes) != len(measurements):
        raise ValueError("scene-change mask does not match the pairs")
    diff_z = np.asarray(
        robust_z([item.frame_difference_rate for item in measurements]),
        dtype=np.float64,
    )
    flow_z = np.asarray(
        robust_z([item.optical_flow_rate for item in measurements]), dtype=np.float64
    )
    used_diff = np.clip(diff_z, 0.0, None)
    used_flow = np.clip(flow_z, 0.0, None)
    for used in (used_diff, used_flow):
        cap = float(np.percentile(used, 95)) if used.size else 0.0
        if cap > 0.0:
            np.minimum(used, cap, out=used)
    scene = np.asarray(list(scene_changes), dtype=bool)
    if scene.size:
        used_diff[scene] = 0.0
        used_flow[scene] = 0.0
    combined = 0.5 * (used_diff + used_flow)
    return diff_z.tolist(), flow_z.tolist(), combined.tolist()


def frame_weights(pair_weights: Sequence[float]) -> list[float]:
    """Give each frame the stronger of the intervals that touch it."""

    count = len(pair_weights)
    if count == 0:
        return []
    weights = [0.0] * (count + 1)
    for index, weight in enumerate(pair_weights):
        value = max(0.0, float(weight))
        weights[index] = max(weights[index], value)
        weights[index + 1] = max(weights[index + 1], value)
    return weights


def activity_box(
    frames: Sequence[np.ndarray], scene_changes: Sequence[bool]
) -> ActivityBox:
    """Box around pixels whose own temporal change is in the top quartile.

    A cut is left out of the map so one panel swap cannot paint the whole
    picture active. The box is padded, then rejected when it is almost the
    whole frame, tiny, or too thin to carry a flow window.
    """

    return activity_box_prepared(_prepare_all(frames), scene_changes)


def activity_box_prepared(
    prepared: Sequence[np.ndarray], scene_changes: Sequence[bool]
) -> ActivityBox:
    height, width = prepared[0].shape
    diffs: list[np.ndarray] = []
    for index in range(1, len(prepared)):
        pair_index = index - 1
        if pair_index < len(scene_changes) and scene_changes[pair_index]:
            continue
        diffs.append(cv2.absdiff(prepared[index - 1], prepared[index]))
    if not diffs:
        return ActivityBox(
            False, "no quiet intervals to locate activity", 0, 0, width, height, 1.0
        )
    activity = np.median(np.stack(diffs, axis=0), axis=0)
    thresh = float(np.percentile(activity, ROI_ACTIVITY_PERCENTILE))
    mask = activity >= thresh
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return ActivityBox(False, "activity mask was empty", 0, 0, width, height, 1.0)
    pad_x = int(round(width * ROI_PAD_FRACTION))
    pad_y = int(round(height * ROI_PAD_FRACTION))
    x0 = max(0, int(xs.min()) - pad_x)
    y0 = max(0, int(ys.min()) - pad_y)
    x1 = min(width, int(xs.max()) + 1 + pad_x)
    y1 = min(height, int(ys.max()) + 1 + pad_y)
    box_w = max(1, x1 - x0)
    box_h = max(1, y1 - y0)
    fraction = (box_w * box_h) / float(width * height)
    box = ActivityBox(True, "activity box", x0, y0, box_w, box_h, fraction)
    if box_w < ROI_MIN_SIDE or box_h < ROI_MIN_SIDE:
        return ActivityBox(
            False,
            "activity box is smaller than a flow window",
            x0,
            y0,
            box_w,
            box_h,
            fraction,
        )
    if fraction < ROI_MIN_AREA_FRACTION:
        return ActivityBox(
            False,
            "activity box covers too little of the frame to trust",
            x0,
            y0,
            box_w,
            box_h,
            fraction,
        )
    if fraction > ROI_MAX_AREA_FRACTION:
        return ActivityBox(
            False,
            "activity covers almost the whole frame",
            x0,
            y0,
            box_w,
            box_h,
            fraction,
        )
    return box


def crop_series(frames: Sequence[np.ndarray], box: ActivityBox) -> list[np.ndarray]:
    prepared = _prepare_all(frames)
    x1 = box.x + box.width
    y1 = box.y + box.height
    return [frame[box.y : y1, box.x : x1] for frame in prepared]


def select_frames(
    times: Sequence[float],
    weights: Sequence[float],
    *,
    budget: int,
    identities: Sequence[str],
) -> list[Chosen]:
    """Baseline coverage, then extra frames in proportion to motion mass.

    Half of the budget (at least the two ends) is spread evenly in real time.
    The remaining slots are apportioned to the gaps between those frames by
    each gap's motion mass, and inside a gap the heavier frames are kept.
    Separation for those extra frames is half the shortest positive interval,
    so adjacent captured frames can both be kept where the mass is high, and
    two timestamps that collide cannot. If mass cannot fill the budget, the
    largest time gaps are split. The same source identity is never kept twice.
    """

    count = len(times)
    if budget <= 0:
        raise ValueError("budget must be positive")
    if count == 0:
        return []
    if not (count == len(weights) == len(identities)):
        raise ValueError("times, weights, and identities differ in length")
    unique = _first_of_each_identity(identities)
    if len(unique) <= budget:
        return [Chosen(index, "all_available") for index in unique]
    if budget == 1:
        return [Chosen(unique[0], "endpoint")]

    chosen: dict[int, str] = {}
    used: set[str] = set()

    def take(index: int, reason: str, *, minimum_gap: float) -> bool:
        if index in chosen or not 0 <= index < count:
            return False
        identity = identities[index]
        if identity and identity in used:
            return False
        if minimum_gap > 0.0 and any(
            abs(float(times[index]) - float(times[other])) < minimum_gap
            for other in chosen
        ):
            return False
        if reason not in _REASONS:
            raise ValueError(reason)
        chosen[index] = reason
        if identity:
            used.add(identity)
        return True

    baseline_n = max(2, min(budget, int(round(budget * BASELINE_FRACTION))))
    take(0, "endpoint", minimum_gap=0.0)
    take(count - 1, "endpoint", minimum_gap=0.0)
    start = float(times[0])
    span = float(times[-1]) - start
    if baseline_n > 2 and span > 0.0:
        for step in range(1, baseline_n - 1):
            target = start + span * (step / (baseline_n - 1))
            nearest = _nearest_free(times, target, chosen, identities, used)
            if nearest is not None:
                take(nearest, "baseline", minimum_gap=0.0)

    minimum_gap = _minimum_gap(times)
    residual = budget - len(chosen)
    if residual > 0:
        _spend_motion(
            times, weights, chosen, used, identities, residual, minimum_gap, take
        )
    while len(chosen) < budget:
        pool = _free_indexes(count, chosen, identities, used)
        if not pool:
            break
        pick = _gap_candidate(times, weights, chosen, pool, minimum_gap)
        if pick is None:
            pick = _gap_candidate(times, weights, chosen, pool, 0.0)
        if pick is None or not take(pick, "gap_fill", minimum_gap=0.0):
            break
    return [Chosen(index, chosen[index]) for index in sorted(chosen)]


def _first_of_each_identity(identities: Sequence[str]) -> list[int]:
    seen: set[str] = set()
    unique: list[int] = []
    for index, identity in enumerate(identities):
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        unique.append(index)
    return unique


def _nearest_free(
    times: Sequence[float],
    target: float,
    chosen: dict[int, str],
    identities: Sequence[str],
    used: set[str],
) -> int | None:
    order = sorted(
        range(len(times)), key=lambda index: (abs(float(times[index]) - target), index)
    )
    for index in order:
        if index in chosen:
            continue
        identity = identities[index]
        if identity and identity in used:
            continue
        return index
    return None


def _minimum_gap(times: Sequence[float]) -> float:
    deltas = [
        float(right) - float(left)
        for left, right in zip(times, times[1:], strict=False)
        if float(right) - float(left) > 1e-6
    ]
    if not deltas:
        return 0.0
    return 0.5 * min(deltas)


def _free_indexes(
    count: int, chosen: dict[int, str], identities: Sequence[str], used: set[str]
) -> list[int]:
    free: list[int] = []
    for index in range(count):
        if index in chosen:
            continue
        identity = identities[index]
        if identity and identity in used:
            continue
        free.append(index)
    return free


def _spend_motion(
    times: Sequence[float],
    weights: Sequence[float],
    chosen: dict[int, str],
    used: set[str],
    identities: Sequence[str],
    residual: int,
    minimum_gap: float,
    take: Any,
) -> None:
    bounds = sorted(chosen)
    segments: list[list[int]] = []
    masses: list[float] = []
    for left, right in zip(bounds, bounds[1:], strict=False):
        interior = [
            index
            for index in range(left + 1, right)
            if index not in chosen
            and not (identities[index] and identities[index] in used)
        ]
        segments.append(interior)
        masses.append(sum(max(0.0, float(weights[index])) for index in interior))
    shares = _apportion(masses, residual)
    for interior, share in zip(segments, shares, strict=True):
        _take_heaviest(interior, share, weights, minimum_gap, take)


def _apportion(masses: Sequence[float], count: int) -> list[int]:
    if count <= 0 or not masses:
        return [0] * len(masses)
    total = float(sum(masses))
    if total <= 1e-12:
        return [0] * len(masses)
    raw = [count * float(mass) / total for mass in masses]
    floors = [int(math.floor(value)) for value in raw]
    left = count - sum(floors)
    order = sorted(
        range(len(masses)),
        key=lambda index: (raw[index] - floors[index], masses[index], -index),
        reverse=True,
    )
    for index in order[:left]:
        floors[index] += 1
    return floors


def _take_heaviest(
    interior: Sequence[int],
    share: int,
    weights: Sequence[float],
    minimum_gap: float,
    take: Any,
) -> None:
    ranked = sorted(interior, key=lambda index: (-float(weights[index]), index))
    placed = 0
    for index in ranked:
        if placed >= share:
            return
        if float(weights[index]) <= 0.0:
            return
        if take(index, "motion", minimum_gap=minimum_gap):
            placed += 1


def _gap_candidate(
    times: Sequence[float],
    weights: Sequence[float],
    chosen: dict[int, str],
    pool: Sequence[int],
    minimum_gap: float,
) -> int | None:
    available = set(pool)
    bounds = sorted(chosen)
    best: tuple[tuple[float, float, float, int], int] | None = None
    for left, right in zip(bounds, bounds[1:], strict=False):
        gap = float(times[right]) - float(times[left])
        midpoint = (float(times[left]) + float(times[right])) / 2.0
        for index in range(left + 1, right):
            if index not in available:
                continue
            if minimum_gap > 0.0 and any(
                abs(float(times[index]) - float(times[other])) < minimum_gap
                for other in chosen
            ):
                continue
            rank = (
                gap,
                float(weights[index]),
                -abs(float(times[index]) - midpoint),
                -index,
            )
            if best is None or rank > best[0]:
                best = (rank, index)
    if best is not None:
        return best[1]
    if minimum_gap > 0.0:
        return None
    if not pool:
        return None
    return min(
        pool,
        key=lambda index: (
            abs(float(times[index]) - float(times[len(times) // 2])),
            index,
        ),
    )


def high_motion_regions(
    ticks: Sequence[int],
    times: Sequence[float],
    combined: Sequence[float],
    *,
    level: float = HIGH_MOTION_Z,
) -> list[dict[str, float | int]]:
    """Contiguous runs whose combined score is above this sequence's typical level."""

    regions: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(combined):
        hot = float(value) >= level
        if hot and start is None:
            start = index
        elif not hot and start is not None:
            regions.append((start, index - 1))
            start = None
    if start is not None:
        regions.append((start, len(combined) - 1))
    described: list[dict[str, float | int]] = []
    for first, last in regions:
        frame_start = first
        frame_end = last + 1
        described.append(
            {
                "start_tick": int(ticks[frame_start]),
                "end_tick": int(ticks[frame_end]),
                "start_seconds": float(times[frame_start]),
                "end_seconds": float(times[frame_end]),
            }
        )
    return described


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    if float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _border_from_magnitudes(magnitudes: Sequence[np.ndarray]) -> dict[str, float]:
    """Share of flow magnitude that sits in the outer margin or the bottom strip.

    This is recorded so a later look can see whether edges dominate. It does
    not change which frames are kept.
    """

    if not magnitudes:
        return {"border_motion_fraction": 0.0, "bottom_strip_motion_fraction": 0.0}
    border_total = 0.0
    bottom_total = 0.0
    height, width = magnitudes[0].shape
    margin_y = max(1, int(round(height * BORDER_MARGIN)))
    margin_x = max(1, int(round(width * BORDER_MARGIN)))
    for magnitude in magnitudes:
        total = float(magnitude.sum()) + 1e-9
        inner = magnitude[margin_y : height - margin_y, margin_x : width - margin_x]
        border_total += max(0.0, float(magnitude.sum()) - float(inner.sum())) / total
        bottom_total += float(magnitude[-margin_y:].sum()) / total
    count = float(len(magnitudes))
    return {
        "border_motion_fraction": border_total / count,
        "bottom_strip_motion_fraction": bottom_total / count,
    }


def _source_box(
    box: ActivityBox, analysis_shape: tuple[int, int], source_size: tuple[int, int]
) -> dict[str, int]:
    analysis_h, analysis_w = analysis_shape
    source_w, source_h = source_size
    if analysis_w <= 0 or analysis_h <= 0:
        return {"x": 0, "y": 0, "width": source_w, "height": source_h}
    return {
        "x": int(round(box.x * source_w / analysis_w)),
        "y": int(round(box.y * source_h / analysis_h)),
        "width": int(round(box.width * source_w / analysis_w)),
        "height": int(round(box.height * source_h / analysis_h)),
    }


def analyze_grays(
    grays: Sequence[np.ndarray],
    times: Sequence[float],
    *,
    budget: int,
    identities: Sequence[str] | None = None,
    ticks: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Score a grayscale series and choose indexes. No files are written."""

    if len(grays) != len(times):
        raise ValueError("frames and timestamps differ in length")
    count = len(grays)
    ident = (
        list(identities)
        if identities is not None
        else [str(index) for index in range(count)]
    )
    tick_list = list(ticks) if ticks is not None else list(range(1, count + 1))
    if not (len(ident) == len(tick_list) == count):
        raise ValueError("identities or ticks do not match the frames")
    full = measure_series(grays, times)
    scene = scene_change_mask(full)
    _full_diff_z, _full_flow_z, full_combined = combined_motion(full, scene)
    box = activity_box(grays, scene)
    signal_name = "full_frame"
    used = full
    roi_combined: list[float] | None = None
    if box.accepted:
        roi = measure_series(crop_series(grays, box), times)
        _roi_diff_z, _roi_flow_z, roi_combined = combined_motion(roi, scene)
        if len(roi_combined) == len(full_combined):
            signal_name = "activity_roi"
            used = roi
    diff_z, flow_z, combined = combined_motion(used, scene)
    chosen, sampling_combined, start, cut = _select_on_useful_sequence(
        times, used, scene, full, ident, budget
    )
    reasons = {item.index: item.reason for item in chosen}
    return {
        "signal": signal_name,
        "scene_changes": scene,
        "full": full,
        "used": used,
        "full_combined": full_combined,
        "combined": combined,
        "diff_z": diff_z,
        "flow_z": flow_z,
        "chosen": chosen,
        "reasons": reasons,
        "box": box,
        "ticks": tick_list,
        "times": [float(value) for value in times],
        "identities": ident,
        "useful_start_index": start,
        "cut_pair_index": cut,
        "sampling_combined": sampling_combined,
        "regions": high_motion_regions(
            tick_list[start:], times[start:], sampling_combined
        ),
        "correlation": None
        if roi_combined is None
        else _pearson(full_combined, roi_combined),
    }


def _decode_gray(path: Path) -> tuple[np.ndarray, int, int]:
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"could not decode {path}")
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    return gray, width, height


def _identity(frame: TimelineFrame) -> str:
    if frame.sha256:
        return frame.sha256
    return frame.relative_file or str(frame.path)


def _cadence(times: Sequence[float]) -> dict[str, float]:
    deltas = [
        (float(right) - float(left)) * 1000.0
        for left, right in zip(times, times[1:], strict=False)
        if float(right) > float(left)
    ]
    if not deltas:
        return {
            "count": 0,
            "min": 0.0,
            "p25": 0.0,
            "median": 0.0,
            "p75": 0.0,
            "max": 0.0,
        }
    data = np.asarray(deltas, dtype=np.float64)
    return {
        "count": float(data.size),
        "min": float(data.min()),
        "p25": float(np.percentile(data, 25)),
        "median": float(np.percentile(data, 50)),
        "p75": float(np.percentile(data, 75)),
        "max": float(data.max()),
    }


def _peak_tick(ticks: Sequence[int], combined: Sequence[float]) -> int | None:
    if not combined:
        return None
    index = int(np.argmax(np.asarray(combined, dtype=np.float64)))
    return int(ticks[index + 1])


def _useful_record(
    frames: Sequence[TimelineFrame],
    times: Sequence[float],
    start: int,
    cut_pair: int | None,
) -> dict[str, Any]:
    origin = float(times[0]) if times else 0.0
    cut_frame = None if cut_pair is None else min(cut_pair + 1, max(0, len(frames) - 1))

    def elapsed(index: int | None) -> float | None:
        if index is None or not times:
            return None
        return float(times[index]) - origin

    return {
        "detected": cut_pair is not None,
        "cut_frame_index": cut_frame,
        "cut_tick": None if cut_frame is None else frames[cut_frame].capture_tick,
        "cut_elapsed_seconds": elapsed(cut_frame),
        "start_index": start if frames else 0,
        "start_tick": frames[start].capture_tick if frames else None,
        "start_elapsed_seconds": elapsed(start if frames else None),
        "excluded_frame_count": start if frames else 0,
    }


def _assemble(
    *,
    frames: Sequence[TimelineFrame],
    sizes: Sequence[tuple[int, int]],
    budget: int,
    camera_id: str,
    shot_id: str,
    signal_name: str,
    box: ActivityBox,
    full: Sequence[PairMeasurement],
    used: Sequence[PairMeasurement],
    full_combined: Sequence[float],
    combined: Sequence[float],
    diff_z: Sequence[float],
    flow_z: Sequence[float],
    scene: Sequence[bool],
    reasons: dict[int, str],
    regions: Sequence[dict[str, float | int]],
    borders: dict[str, float],
    correlation: float | None,
    timing: dict[str, float],
    source_size: tuple[int, int],
    analysis_shape: tuple[int, int],
    useful_start: int = 0,
    cut_pair: int | None = None,
) -> dict[str, Any]:
    times = [frame.offset_seconds for frame in frames]
    origin = times[0] if times else 0.0
    rows: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        pair = None if index == 0 else used[index - 1]
        full_pair = None if index == 0 else full[index - 1]
        width, height = sizes[index]
        rows.append(
            {
                "capture_tick": frame.capture_tick,
                "timestamp_offset_seconds": frame.offset_seconds,
                "elapsed_seconds": frame.offset_seconds - origin,
                "replay_seconds": frame.replay_seconds,
                "requested_at": frame.requested_at,
                "delta_seconds": None if pair is None else pair.delta_seconds,
                "frame_difference_score": None
                if pair is None
                else pair.frame_difference_score,
                "frame_difference_rate": None
                if pair is None
                else pair.frame_difference_rate,
                "frame_difference_z": None if pair is None else diff_z[index - 1],
                "optical_flow_displacement": None
                if pair is None
                else pair.optical_flow_displacement,
                "optical_flow_rate": None if pair is None else pair.optical_flow_rate,
                "optical_flow_z": None if pair is None else flow_z[index - 1],
                "combined_motion": None if pair is None else combined[index - 1],
                "full_frame_difference_rate": None
                if full_pair is None
                else full_pair.frame_difference_rate,
                "full_optical_flow_rate": None
                if full_pair is None
                else full_pair.optical_flow_rate,
                "full_combined_motion": None
                if index == 0
                else full_combined[index - 1],
                "changed_fraction": None
                if full_pair is None
                else full_pair.changed_fraction,
                "scene_change": False if index == 0 else bool(scene[index - 1]),
                "nonpositive_dt": False if pair is None else pair.nonpositive_dt,
                "selected": index in reasons,
                "selection_reason": reasons.get(index),
                "in_useful_sequence": index >= useful_start,
                "useful_start": cut_pair is not None and index == useful_start,
                "file": frame.relative_file,
                "path": frame.path.as_posix(),
                "width": width,
                "height": height,
                "sha256": frame.sha256,
            }
        )
    selected_rows = [row for row in rows if row["selected"]]
    manifest_frames = [
        {
            "order": order,
            "capture_tick": row["capture_tick"],
            "timestamp_offset_seconds": row["timestamp_offset_seconds"],
            "elapsed_seconds": row["elapsed_seconds"],
            "replay_seconds": row["replay_seconds"],
            "selection_reason": row["selection_reason"],
            "file": row["file"],
            "path": row["path"],
            "width": row["width"],
            "height": row["height"],
            "sha256": row["sha256"],
        }
        for order, row in enumerate(selected_rows)
    ]
    return {
        "shot_id": shot_id,
        "camera": camera_id,
        "requested_budget": budget,
        "selected_count": len(manifest_frames),
        "frame_count": len(frames),
        "time_start_seconds": times[0] if times else None,
        "time_end_seconds": times[-1] if times else None,
        "duration_seconds": (times[-1] - times[0]) if len(times) > 1 else 0.0,
        "cadence_ms": _cadence(times),
        "useful_sequence": _useful_record(frames, times, useful_start, cut_pair),
        "sampling_signal": signal_name,
        "roi": {
            "accepted": box.accepted,
            "reason": box.reason,
            "analysis_box": {
                "x": box.x,
                "y": box.y,
                "width": box.width,
                "height": box.height,
            },
            "source_box": _source_box(box, analysis_shape, source_size),
            "area_fraction": box.area_fraction,
            "full_vs_roi_combined_correlation": correlation,
            "full_peak_tick": _peak_tick(
                [frame.capture_tick for frame in frames], full_combined
            ),
            "roi_peak_tick": None
            if signal_name != "activity_roi"
            else _peak_tick([frame.capture_tick for frame in frames], combined),
        },
        "border_motion": borders,
        "high_motion_regions": list(regions),
        "selected_ticks": [row["capture_tick"] for row in manifest_frames],
        "selection_reasons": {
            reason: sum(
                1 for row in manifest_frames if row["selection_reason"] == reason
            )
            for reason in sorted({row["selection_reason"] for row in manifest_frames})
        },
        "algorithm": {
            "analysis_width": ANALYSIS_WIDTH,
            "upper_quantile": UPPER_QUANTILE,
            "baseline_fraction": BASELINE_FRACTION,
            "scene_change_fraction": SCENE_CHANGE_FRACTION,
            "pixel_change_floor": PIXEL_CHANGE_FLOOR,
            "optical_flow": "farneback",
            "high_motion_z": HIGH_MOTION_Z,
        },
        "timing_seconds": timing,
        "frames": rows,
        "manifest_frames": manifest_frames,
    }


def _crop_prepared(
    prepared: Sequence[np.ndarray], box: ActivityBox
) -> list[np.ndarray]:
    x1 = box.x + box.width
    y1 = box.y + box.height
    return [frame[box.y : y1, box.x : x1] for frame in prepared]


def run_timeline(
    frames: Sequence[TimelineFrame],
    *,
    budget: int = DEFAULT_BUDGET,
    camera_id: str = "swing_behind",
    shot_id: str = "",
) -> dict[str, Any]:
    """Score `frames` once per signal, and again inside an accepted activity box."""

    if budget <= 0:
        raise ValueError("budget must be positive")
    started = time.perf_counter()
    grays: list[np.ndarray] = []
    sizes: list[tuple[int, int]] = []
    load_started = time.perf_counter()
    for frame in frames:
        gray, width, height = _decode_gray(frame.path)
        grays.append(gray)
        sizes.append((width, height))
    prepared = _prepare_all(grays) if grays else []
    load_seconds = time.perf_counter() - load_started
    times = [frame.offset_seconds for frame in frames]
    if prepared:
        full, magnitudes, diff_seconds, flow_seconds = _score_prepared(prepared, times)
    else:
        full = []
        magnitudes = []
        diff_seconds = 0.0
        flow_seconds = 0.0
    activity_started = time.perf_counter()
    scene = scene_change_mask(full)
    box = (
        activity_box_prepared(prepared, scene)
        if prepared
        else ActivityBox(False, "no frames", 0, 0, 0, 0, 0.0)
    )
    diff_seconds += time.perf_counter() - activity_started
    full_combined = combined_motion(full, scene)[2]
    signal_name = "full_frame"
    used = full
    roi_combined: list[float] | None = None
    if box.accepted:
        roi, _roi_magnitudes, roi_diff, roi_flow = _score_prepared(
            _crop_prepared(prepared, box), times
        )
        diff_seconds += roi_diff
        flow_seconds += roi_flow
        roi_combined = combined_motion(roi, scene)[2]
        if len(roi_combined) == len(full_combined):
            signal_name = "activity_roi"
            used = roi
    select_started = time.perf_counter()
    diff_z, flow_z, combined = combined_motion(used, scene)
    identities = [_identity(frame) for frame in frames]
    ticks = [frame.capture_tick for frame in frames]
    chosen, sampling_combined, start, cut = _select_on_useful_sequence(
        times, used, scene, full, identities, budget
    )
    reasons = {item.index: item.reason for item in chosen}
    regions = high_motion_regions(ticks[start:], times[start:], sampling_combined)
    borders = _border_from_magnitudes(magnitudes)
    select_seconds = time.perf_counter() - select_started
    source_size = sizes[0] if sizes else (0, 0)
    analysis_shape = prepared[0].shape if prepared else (0, 0)
    result = _assemble(
        frames=frames,
        sizes=sizes,
        budget=budget,
        camera_id=camera_id,
        shot_id=shot_id,
        signal_name=signal_name,
        box=box,
        full=full,
        used=used,
        full_combined=full_combined,
        combined=combined,
        diff_z=diff_z,
        flow_z=flow_z,
        scene=scene,
        reasons=reasons,
        regions=regions,
        borders=borders,
        correlation=_pearson(full_combined, roi_combined)
        if roi_combined is not None
        else None,
        timing={
            "image_loading": load_seconds,
            "frame_difference": diff_seconds,
            "optical_flow": flow_seconds,
            "selection": select_seconds,
            "total": time.perf_counter() - started,
        },
        source_size=source_size,
        analysis_shape=analysis_shape,
        useful_start=start,
        cut_pair=cut,
    )
    return result


def write_outputs(
    result: dict[str, Any], directory: Path, frames: Sequence[TimelineFrame]
) -> None:
    """Write the score table, the curve, the sheet, and the frame manifest."""

    directory.mkdir(parents=True, exist_ok=True)
    scores = {key: value for key, value in result.items() if not key.startswith("_")}
    scores.pop("manifest_frames", None)
    (directory / "motion_scores.json").write_text(
        json.dumps(_jsonable(scores), indent=2),
        encoding="utf-8",
    )
    manifest = {
        "shot_id": result["shot_id"],
        "camera": result["camera"],
        "requested_budget": result["requested_budget"],
        "selected_count": result["selected_count"],
        "sampling_signal": result["sampling_signal"],
        "useful_sequence": result.get("useful_sequence"),
        "frames": result["manifest_frames"],
    }
    (directory / "selected_frames.json").write_text(
        json.dumps(_jsonable(manifest), indent=2),
        encoding="utf-8",
    )
    _draw_curve(result, directory / "motion_curve.png")
    _draw_sheet(result, frames, directory / "selected_sheet.jpg")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    if isinstance(value, np.floating):
        return round(float(value), 6)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _sequence_marks(
    result: dict[str, Any],
) -> list[tuple[float, str, tuple[int, int, int]]]:
    info = result.get("useful_sequence") or {}
    if not info.get("detected"):
        return []
    cut_at = info.get("cut_elapsed_seconds")
    start_at = info.get("start_elapsed_seconds")
    cut_ok = isinstance(cut_at, int | float)
    start_ok = isinstance(start_at, int | float)
    if cut_ok and start_ok and abs(float(cut_at) - float(start_at)) <= 1e-6:
        return [(float(start_at), "cut / useful start", (20, 70, 140))]
    marks: list[tuple[float, str, tuple[int, int, int]]] = []
    if cut_ok:
        marks.append((float(cut_at), "replay cut", (170, 40, 40)))
    if start_ok:
        marks.append((float(start_at), "useful start", (20, 70, 140)))
    return marks


def _draw_curve(result: dict[str, Any], path: Path) -> None:
    rows = result["frames"]
    points = [row for row in rows if row["delta_seconds"] is not None]
    width, height = 1100, 660
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(14)
    small = _font(12)
    if len(points) < 2:
        draw.text((24, 24), "not enough frames to plot", fill=(0, 0, 0), font=font)
        image.save(path)
        return
    times = [float(row["elapsed_seconds"]) for row in points]
    t0 = min(float(row["elapsed_seconds"]) for row in rows)
    t1 = max(float(row["elapsed_seconds"]) for row in rows)
    if t1 <= t0:
        t1 = t0 + 1.0
    panels = (
        (70, "frame difference rate", "frame_difference_rate", (32, 96, 180)),
        (360, "optical flow rate", "optical_flow_rate", (196, 92, 28)),
    )
    selected = [row for row in rows if row["selected"]]
    marks = _sequence_marks(result)
    for top, title, key, color in panels:
        values = [float(row[key]) for row in points]
        cap = float(np.percentile(values, 95)) if values else 1.0
        cap = cap if cap > 1e-9 else max(values) if values else 1.0
        cap = cap if cap > 1e-9 else 1.0
        _panel(
            draw,
            times,
            values,
            selected,
            t0,
            t1,
            top,
            title,
            color,
            cap,
            font,
            small,
            marks,
        )
    draw.text(
        (70, 600),
        "x is seconds from the first frame. y is clipped at the 95th percentile for display.",
        fill=(40, 40, 40),
        font=small,
    )
    if marks:
        draw.text(
            (70, 618),
            "shaded time is before the useful sequence. red = replay cut, blue = useful start.",
            fill=(40, 40, 40),
            font=small,
        )
    image.save(path)


def _panel(
    draw: ImageDraw.ImageDraw,
    times: Sequence[float],
    values: Sequence[float],
    selected: Sequence[dict[str, Any]],
    t0: float,
    t1: float,
    top: int,
    title: str,
    color: tuple[int, int, int],
    cap: float,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    small: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    marks: Sequence[tuple[float, str, tuple[int, int, int]]] = (),
) -> None:
    left, right, bottom = 70, 1040, top + 220

    def xy(time_value: float, sample: float) -> tuple[float, float]:
        x = left + (time_value - t0) / (t1 - t0) * (right - left)
        y = bottom - min(sample, cap) / cap * (bottom - top - 8)
        return x, y

    useful_at = [item[0] for item in marks if "useful start" in item[1]]
    if useful_at:
        edge = xy(useful_at[0], 0.0)[0]
        if edge > left + 1:
            draw.rectangle((left, top, edge, bottom), fill=(236, 236, 236))
    draw.rectangle((left, top, right, bottom), outline=(180, 180, 180))
    draw.text((left, top - 22), title, fill=color, font=font)
    line = [
        xy(time_value, sample) for time_value, sample in zip(times, values, strict=True)
    ]
    draw.line(line, fill=color, width=2)
    for row in selected:
        x, _y = xy(float(row["elapsed_seconds"]), 0.0)
        draw.line((x, top, x, bottom), fill=(186, 214, 176), width=1)
        draw.ellipse((x - 3, top + 4, x + 3, top + 10), fill=(20, 130, 50))
    for offset, (when, label, mark_color) in enumerate(marks):
        x, _y = xy(when, 0.0)
        draw.line((x, top, x, bottom), fill=mark_color, width=2)
        draw.text((x + 4, top + 4 + offset * 14), label, fill=mark_color, font=small)
    draw.text((left, bottom + 4), f"{t0:.2f}s", fill=(20, 20, 20), font=small)
    draw.text((right - 48, bottom + 4), f"{t1:.2f}s", fill=(20, 20, 20), font=small)
    draw.text((8, top), f"{cap:.2f}", fill=(20, 20, 20), font=small)


def _draw_sheet(
    result: dict[str, Any], frames: Sequence[TimelineFrame], path: Path
) -> None:
    chosen = [
        frame
        for frame, row in zip(frames, result["frames"], strict=True)
        if row["selected"]
    ]
    rows = [row for row in result["frames"] if row["selected"]]
    if not chosen:
        Image.new("RGB", (320, 80), "white").save(path, quality=90)
        return
    with Image.open(chosen[0].path) as first:
        source_w, source_h = first.size
    columns = 6 if len(chosen) >= 18 else 4 if len(chosen) >= 8 else max(1, len(chosen))
    row_count = math.ceil(len(chosen) / columns)
    cell_w = 240
    cell_h = max(1, int(round(cell_w * (source_h / source_w))))
    label_h = 52
    pad = 8
    sheet = Image.new(
        "RGB",
        (
            columns * cell_w + (columns + 1) * pad,
            row_count * (cell_h + label_h) + (row_count + 1) * pad,
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = _font(14)
    for index, (frame, row) in enumerate(zip(chosen, rows, strict=True)):
        column = index % columns
        grid_row = index // columns
        x = pad + column * (cell_w + pad)
        y = pad + grid_row * (cell_h + label_h + pad)
        with Image.open(frame.path) as image:
            rgb = image.convert("RGB")
            rgb.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
            offset_x = x + (cell_w - rgb.width) // 2
            offset_y = y + (cell_h - rgb.height) // 2
            sheet.paste(rgb, (offset_x, offset_y))
        reason = str(row["selection_reason"] or "")
        draw.text(
            (x, y + cell_h + 2),
            f"tick {row['capture_tick']}",
            fill=(0, 0, 0),
            font=font,
        )
        draw.text(
            (x, y + cell_h + 18),
            f"+{float(row['elapsed_seconds']):.3f}s",
            fill=(0, 0, 0),
            font=font,
        )
        draw.text((x, y + cell_h + 34), reason, fill=(0, 0, 0), font=font)
    sheet.save(path, quality=90)


def run_shot(
    shot_dir: Path,
    *,
    budget: int = DEFAULT_BUDGET,
    camera_id: str = "swing_behind",
    results_root: Path = RESULTS,
) -> Path:
    """Sample one camera and write the research directory. Returns that directory."""

    frames = load_timeline(shot_dir, camera_id)
    result = run_timeline(
        frames,
        budget=budget,
        camera_id=camera_id,
        shot_id=shot_dir.name,
    )
    directory = results_root / shot_dir.name
    if camera_id != "swing_behind":
        directory = directory / camera_id
    write_outputs(result, directory, frames)
    _print_summary(result, directory)
    return directory


def _print_summary(result: dict[str, Any], directory: Path) -> None:
    cadence = result["cadence_ms"]
    timing = result["timing_seconds"]
    print(f"shot {result['shot_id']} camera {result['camera']}")
    print(
        f"frames {result['frame_count']}  "
        f"time {result['time_start_seconds']:.3f}..{result['time_end_seconds']:.3f}s  "
        f"span {result['duration_seconds']:.3f}s"
    )
    print(
        "cadence ms "
        f"min {cadence['min']:.1f}  median {cadence['median']:.1f}  max {cadence['max']:.1f}"
    )
    useful = result.get("useful_sequence") or {}
    print(
        f"useful start tick {useful.get('start_tick')}  "
        f"cut tick {useful.get('cut_tick')}  "
        f"excluded {useful.get('excluded_frame_count')}"
    )
    print(f"signal {result['sampling_signal']}  roi {result['roi']['reason']}")
    print(f"high-motion {result['high_motion_regions']}")
    print(f"selected {result['selected_ticks']}")
    print(f"reasons {result['selection_reasons']}")
    print(
        "seconds "
        f"load {timing['image_loading']:.3f}  "
        f"diff {timing['frame_difference']:.3f}  "
        f"flow {timing['optical_flow']:.3f}  "
        f"select {timing['selection']:.3f}  "
        f"total {timing['total']:.3f}"
    )
    print(f"wrote {directory}")


def _resolve_shot(shot_id: str, root: Path | None) -> Path:
    base = CAPTURES if root is None else root
    path = base / shot_id
    if not path.is_dir():
        raise SystemExit(f"shot not found: {path}")
    return path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Sample original frames by visual motion."
    )
    parser.add_argument("shot_id")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--camera", default="swing_behind")
    parser.add_argument("--shots-root", type=Path, default=None)
    parser.add_argument("--results-root", type=Path, default=RESULTS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.budget <= 0:
        raise SystemExit("budget must be positive")
    run_shot(
        _resolve_shot(args.shot_id, args.shots_root),
        budget=args.budget,
        camera_id=args.camera,
        results_root=args.results_root,
    )


if __name__ == "__main__":
    main()
