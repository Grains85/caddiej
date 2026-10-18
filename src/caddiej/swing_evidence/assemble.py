"""Build a SwingEvidencePackage from capture, the frozen sampler, and stage 3.

The sampler and the stage detector are used as they are. This module decides
which of their outputs to keep inside a shared image budget, and it records
an impact estimate when the ball estimator supports one. It does not name
faults, call a model, or write coaching.

Original frames are only read. Artifacts go under a versioned results directory.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\evidence_package.py \\
        <shot-id> [<shot-id> ...]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from caddiej.swing_evidence.contracts import (
    CONFIDENCE_SCALE,
    IMPACT_ESTIMATED,
    IMPACT_UNRESOLVED,
    SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    UNSUPPORTED_ESTIMATE,
    CameraClockComparison,
    EvidenceAllocation,
    EvidenceFrame,
    FrameReference,
    ImpactEstimate,
    Provenance,
    StageBoundary,
    SwingEvidencePackage,
    SwingStageResult,
    to_plain,
)
from caddiej.swing_evidence.images import decode_frame, require_opencv4
from caddiej.swing_evidence.impact import METHOD, estimate_ball_motion, fuse_impact
from caddiej.swing_evidence.motion import TimelineFrame, load_timeline, run_timeline
from caddiej.swing_evidence.pose_model import active_model_path
from caddiej.swing_evidence.stages import (
    detect_swing_stages,
    load_camera_frames,
    track_poses,
)

PRODUCER = "evidence_package"
PRODUCER_VERSION = "1"
DEFAULT_BUDGET = 24
DEFAULT_PRESET = "dtl_primary"
CAMERAS = ("swing_behind", "swing_side")
DTL = "swing_behind"
FO = "swing_side"
# Counts at a 24-image budget. `resolve_allocation` scales them when the
# budget changes. They are view weights, not fault weights.
ALLOCATION_PRESETS: dict[str, dict[str, int]] = {
    "dtl_only": {DTL: 24, FO: 0},
    "dtl_primary": {DTL: 20, FO: 4},
    "balanced": {DTL: 12, FO: 12},
    "fo_primary": {DTL: 8, FO: 16},
}
_DROP = (
    "gap_fill",
    "baseline",
    "motion",
    "stage_coverage",
    "impact_interval",
    "sequence_start",
    "all_available",
    "endpoint",
)
_SAMPLER_REASONS = frozenset(
    {"endpoint", "baseline", "motion", "gap_fill", "all_available", "sequence_start"}
)
_REPO = Path(__file__).resolve().parents[3]
_SHOTS = _REPO / "research" / "temporal_evidence" / "fresh_v2" / "shots"
_RESULTS = (
    _REPO / "research" / "direct_visual_coach" / "results" / "evidence_pipeline" / "v1"
)
_PROTEE_NOTE = (
    "ProTee ShotDetected is not used. It is not an exact contact time on "
    "the replay frame clock, and this package does not treat it as one."
)
_SYNC_NOTE = (
    "Cameras are compared by capture tick on offset_seconds. "
    "A shared tick is not treated as proof of synchronization."
)


@dataclass(frozen=True, slots=True)
class CandidateFrame:
    camera_id: str
    capture_tick: int
    timestamp_seconds: float
    relative_file: str
    sha256: str
    width: int
    height: int
    in_useful_sequence: bool
    sampler_reason: str | None


def allocate_budgets(camera_ids: Sequence[str], total: int) -> dict[str, int]:
    """Split a total image budget across the cameras that actually exist.

    The shares differ by at most one frame. The extra frames go to the later
    camera ids so the split does not prefer a view.
    """

    if total <= 0:
        raise ValueError("image budget must be positive")
    ids = sorted({camera_id for camera_id in camera_ids if camera_id})
    if not ids:
        return {}
    base, extra = divmod(total, len(ids))
    shares = {camera_id: base for camera_id in ids}
    if extra:
        for camera_id in ids[-extra:]:
            shares[camera_id] += 1
    return shares


def resolve_allocation(
    available: Sequence[str],
    budget: int,
    *,
    preset: str | None = None,
    shares: Mapping[str, int] | None = None,
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Turn a preset or an explicit split into per-camera budgets.

    A camera that is not in `available` does not keep a slot. Its images move
    to the cameras that are present. A preset share of zero stays zero when
    that camera is present: `dtl_only` does not invent face-on frames.
    """

    if budget <= 0:
        raise ValueError("image budget must be positive")
    if shares is not None and preset is not None:
        raise ValueError("pass a preset or an explicit split, not both")
    chosen = DEFAULT_PRESET if preset is None and shares is None else preset
    if shares is not None:
        weights = _checked_shares(shares, budget)
    else:
        if chosen not in ALLOCATION_PRESETS:
            raise ValueError(f"unknown allocation preset {chosen!r}")
        weights = _scale_weights(ALLOCATION_PRESETS[chosen], budget)
    present = {camera_id for camera_id in available if camera_id}
    missing = tuple(
        camera_id
        for camera_id, count in weights.items()
        if count > 0 and camera_id not in present
    )
    kept = {
        camera_id: count
        for camera_id, count in weights.items()
        if count > 0 and camera_id in present
    }
    if not kept:
        fallback = allocate_budgets(tuple(present), budget)
        return _with_declared_zeros(fallback, weights, present), missing
    if sum(kept.values()) != budget:
        kept = _scale_weights(kept, budget)
    return _with_declared_zeros(kept, weights, present), missing


def _with_declared_zeros(
    shares: dict[str, int], weights: Mapping[str, int], present: set[str]
) -> dict[str, int]:
    merged = dict(shares)
    for camera_id, count in weights.items():
        if camera_id in present and int(count) == 0:
            merged.setdefault(camera_id, 0)
    return merged


def _checked_shares(shares: Mapping[str, int], budget: int) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for camera_id, count in shares.items():
        if camera_id not in CAMERAS:
            raise ValueError(f"unknown camera {camera_id!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{camera_id} share must be a non-negative integer")
        cleaned[camera_id] = count
    if sum(cleaned.values()) != budget:
        raise ValueError("camera shares must add up to the image budget")
    return cleaned


def _scale_weights(weights: Mapping[str, int], budget: int) -> dict[str, int]:
    total = sum(int(value) for value in weights.values())
    if total <= 0:
        raise ValueError("allocation weights must be positive")
    raw = {key: budget * (int(weights[key]) / total) for key in weights}
    floors = {key: int(raw[key]) for key in weights}
    leftover = budget - sum(floors.values())
    order = sorted(
        weights,
        key=lambda key: (raw[key] - floors[key], key),
        reverse=True,
    )
    for key in order:
        if leftover <= 0:
            break
        if int(weights[key]) <= 0:
            continue
        floors[key] += 1
        leftover -= 1
    return floors


def compare_clocks(
    frames_by_camera: Mapping[str, Sequence[TimelineFrame]],
) -> CameraClockComparison:
    """Report the capture-clock gap between two cameras. Do not invent a sync."""

    cameras = [camera for camera, frames in frames_by_camera.items() if frames]
    if len(cameras) < 2:
        return CameraClockComparison(
            matched_ticks=0,
            median_abs_delta_seconds=None,
            max_abs_delta_seconds=None,
            sufficient=False,
            note="One camera is present, so there is nothing to synchronize.",
        )
    left, right = sorted(cameras)[:2]
    by_tick = {
        frame.capture_tick: frame.offset_seconds for frame in frames_by_camera[right]
    }
    deltas = [
        abs(frame.offset_seconds - by_tick[frame.capture_tick])
        for frame in frames_by_camera[left]
        if frame.capture_tick in by_tick
    ]
    if not deltas:
        return CameraClockComparison(
            matched_ticks=0,
            median_abs_delta_seconds=None,
            max_abs_delta_seconds=None,
            sufficient=False,
            note="No shared capture tick, so the cameras cannot be compared.",
        )
    ordered = sorted(deltas)
    median = ordered[len(ordered) // 2]
    farthest = ordered[-1]
    return CameraClockComparison(
        matched_ticks=len(deltas),
        median_abs_delta_seconds=median,
        max_abs_delta_seconds=farthest,
        sufficient=farthest <= 0.05,
        note=_SYNC_NOTE,
    )


def frames_that_decode(
    frames: Sequence[TimelineFrame],
) -> tuple[tuple[TimelineFrame, ...], tuple[str, ...]]:
    """Drop frames that are missing, empty, or not a readable image."""

    kept: list[TimelineFrame] = []
    rejected: list[str] = []
    for frame in frames:
        if not frame.path.is_file() or frame.path.stat().st_size < 32:
            rejected.append(frame.relative_file or str(frame.path))
            continue
        if decode_frame(frame.path) is None:
            rejected.append(frame.relative_file or str(frame.path))
            continue
        kept.append(frame)
    return tuple(kept), tuple(rejected)


def duplicate_count(frames: Sequence[TimelineFrame]) -> int:
    seen: set[str] = set()
    duplicates = 0
    for frame in frames:
        identity = frame.sha256 or frame.relative_file
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
    return duplicates


def choose_frames(
    frames: Sequence[CandidateFrame],
    *,
    per_camera_budget: Mapping[str, int],
    boundaries: Sequence[StageBoundary],
    boundary_cameras: Sequence[str],
    impact: ImpactEstimate | None,
) -> tuple[tuple[EvidenceFrame, ...], str, tuple[str, ...]]:
    """Start from the sampler, then cover accepted stages and a real impact interval.

    Unresolved stages and an unresolved impact add nothing. A frame keeps the
    sampler's reason when the sampler already selected it.
    """

    if len(boundaries) != len(boundary_cameras):
        raise ValueError("each stage boundary needs the camera it came from")
    warnings: list[str] = []
    selected: dict[tuple[str, int], CandidateFrame] = {}
    reasons: dict[tuple[str, int], str] = {}
    for frame in frames:
        if frame.sampler_reason not in _SAMPLER_REASONS:
            continue
        key = (frame.camera_id, frame.capture_tick)
        if key in selected or _same_image(frame, selected):
            continue
        selected[key] = frame
        reasons[key] = frame.sampler_reason or "motion"
    for camera_id, limit in per_camera_budget.items():
        while sum(1 for key in selected if key[0] == camera_id) > limit:
            dropped = _drop_one(selected, reasons, camera_id)
            if dropped is None:
                warnings.append(f"{camera_id} stayed above its image budget")
                break
            del selected[dropped]
            del reasons[dropped]

    stage_added = False
    paired = list(zip(boundary_cameras, boundaries, strict=True))
    for camera_id, boundary in paired:
        if _covered(selected, reasons, camera_id, boundary.time_interval_seconds):
            continue
        pick = _nearest_in_interval(
            frames, selected, camera_id, boundary.time_interval_seconds
        )
        if pick is None:
            warnings.append(
                f"no useful {camera_id} frame inside accepted {boundary.stage}"
            )
            continue
        if _insert(selected, reasons, pick, "stage_coverage", per_camera_budget):
            stage_added = True
        else:
            warnings.append(
                f"{camera_id} budget could not hold a {boundary.stage} frame"
            )

    impact_added = False
    window = _impact_window(impact)
    if window is not None:
        for camera_id in sorted({frame.camera_id for frame in frames}):
            inside = [
                frame
                for frame in frames
                if frame.camera_id == camera_id
                and frame.in_useful_sequence
                and window[0] <= frame.timestamp_seconds <= window[1]
            ]
            already = [
                frame
                for frame in inside
                if (frame.camera_id, frame.capture_tick) in selected
            ]
            need = min(2, len(inside))
            for frame in inside:
                if len(already) >= need:
                    break
                key = (frame.camera_id, frame.capture_tick)
                if key in selected:
                    continue
                if _insert(
                    selected, reasons, frame, "impact_interval", per_camera_budget
                ):
                    already.append(frame)
                    impact_added = True
            if need and len(already) < need:
                warnings.append(
                    f"{camera_id} budget could not add frames around ball departure"
                )
    elif impact is not None and impact.status != IMPACT_ESTIMATED:
        warnings.append("impact is unresolved, so no frame was added for contact")

    ordered = sorted(
        selected.values(),
        key=lambda frame: (
            frame.timestamp_seconds,
            frame.camera_id,
            frame.capture_tick,
        ),
    )
    evidence: list[EvidenceFrame] = []
    for order, frame in enumerate(ordered):
        evidence.append(
            EvidenceFrame(
                reference=FrameReference(
                    camera_id=frame.camera_id,
                    relative_file=frame.relative_file,
                    capture_tick=frame.capture_tick,
                    timestamp_seconds=frame.timestamp_seconds,
                    sha256=frame.sha256 or None,
                    width=frame.width or None,
                    height=frame.height or None,
                ),
                order=order,
                selection_reason=reasons[(frame.camera_id, frame.capture_tick)],
                stage=_stage_name(frame, paired),
            )
        )
    fallback = _fallback(
        stage_added, impact_added, bool(boundaries), window is not None
    )
    return tuple(evidence), fallback, tuple(warnings)


def _fallback(
    stage_added: bool, impact_added: bool, had_stages: bool, had_impact: bool
) -> str:
    if impact_added and (stage_added or had_stages):
        return "stage_and_impact"
    if impact_added:
        return "impact_density"
    if stage_added or had_stages:
        return "stage_coverage"
    return "generic_motion"


def _impact_window(impact: ImpactEstimate | None) -> tuple[float, float] | None:
    if impact is None or impact.status != IMPACT_ESTIMATED:
        return None
    span = impact.contact_interval_seconds
    if span is None or span[1] < span[0]:
        return None
    return span


def _covered(
    selected: Mapping[tuple[str, int], CandidateFrame],
    reasons: Mapping[tuple[str, int], str],
    camera_id: str,
    interval: tuple[float, float],
) -> bool:
    del reasons
    return any(
        frame.camera_id == camera_id
        and interval[0] <= frame.timestamp_seconds <= interval[1]
        for frame in selected.values()
    )


def _nearest_in_interval(
    frames: Sequence[CandidateFrame],
    selected: Mapping[tuple[str, int], CandidateFrame],
    camera_id: str,
    interval: tuple[float, float],
) -> CandidateFrame | None:
    midpoint = (interval[0] + interval[1]) / 2.0
    pool = [
        frame
        for frame in frames
        if frame.camera_id == camera_id
        and frame.in_useful_sequence
        and interval[0] <= frame.timestamp_seconds <= interval[1]
        and (frame.camera_id, frame.capture_tick) not in selected
        and not _same_image(frame, selected)
    ]
    if not pool:
        return None
    pool.sort(
        key=lambda frame: (abs(frame.timestamp_seconds - midpoint), frame.capture_tick)
    )
    return pool[0]


def _same_image(
    frame: CandidateFrame, selected: Mapping[tuple[str, int], CandidateFrame]
) -> bool:
    if not frame.sha256:
        return False
    return any(other.sha256 == frame.sha256 for other in selected.values())


def _insert(
    selected: dict[tuple[str, int], CandidateFrame],
    reasons: dict[tuple[str, int], str],
    frame: CandidateFrame,
    reason: str,
    per_camera_budget: Mapping[str, int],
) -> bool:
    key = (frame.camera_id, frame.capture_tick)
    limit = per_camera_budget.get(frame.camera_id, 0)
    if limit <= 0:
        return False
    count = sum(1 for camera_id, _tick in selected if camera_id == frame.camera_id)
    if count >= limit:
        dropped = _drop_one(selected, reasons, frame.camera_id)
        if dropped is None:
            return False
        del selected[dropped]
        del reasons[dropped]
    selected[key] = frame
    reasons[key] = reason
    return True


def _drop_one(
    selected: Mapping[tuple[str, int], CandidateFrame],
    reasons: Mapping[tuple[str, int], str],
    camera_id: str,
) -> tuple[str, int] | None:
    for reason in _DROP:
        if reason in {
            "stage_coverage",
            "impact_interval",
            "endpoint",
            "sequence_start",
        }:
            continue
        matches = [
            key
            for key, label in reasons.items()
            if key[0] == camera_id and label == reason
        ]
        if matches:
            return matches[-1]
    return None


def _stage_name(
    frame: CandidateFrame,
    boundaries: Sequence[tuple[str, StageBoundary]],
) -> str | None:
    matches = [
        boundary.stage
        for camera_id, boundary in boundaries
        if camera_id == frame.camera_id
        and boundary.time_interval_seconds[0]
        <= frame.timestamp_seconds
        <= boundary.time_interval_seconds[1]
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _candidates(
    camera_id: str, rows: Sequence[Mapping[str, Any]]
) -> list[CandidateFrame]:
    frames: list[CandidateFrame] = []
    for row in rows:
        reason = row.get("selection_reason") if row.get("selected") else None
        if reason is not None and reason not in _SAMPLER_REASONS:
            reason = None
        frames.append(
            CandidateFrame(
                camera_id=camera_id,
                capture_tick=int(row["capture_tick"]),
                timestamp_seconds=float(row["timestamp_offset_seconds"]),
                relative_file=str(row["file"]),
                sha256=str(row.get("sha256") or ""),
                width=int(row.get("width") or 0),
                height=int(row.get("height") or 0),
                in_useful_sequence=bool(row.get("in_useful_sequence")),
                sampler_reason=reason,
            )
        )
    return frames


def _high_motion_coverage(
    sampled: Mapping[str, Mapping[str, Any]],
    chosen: Sequence[EvidenceFrame],
) -> dict[str, Any]:
    chosen_ticks = {
        (frame.reference.camera_id, frame.reference.capture_tick) for frame in chosen
    }
    coverage: dict[str, Any] = {}
    for camera_id, result in sampled.items():
        regions = result.get("high_motion_regions") or []
        ticks: list[int] = []
        for region in regions:
            start = int(region["start_tick"])
            end = int(region["end_tick"])
            ticks.extend(range(start, end + 1))
        if not ticks:
            coverage[camera_id] = {"regions": 0, "ticks": 0, "selected_ticks": 0}
            continue
        hit = sum(1 for tick in ticks if (camera_id, tick) in chosen_ticks)
        coverage[camera_id] = {
            "regions": len(regions),
            "ticks": len(ticks),
            "selected_ticks": hit,
        }
    return coverage


class ShotSuperseded(Exception):
    """A newer shot arrived while this one was still being read.

    Raised only when the caller passed a cancellation check. The research
    command does not pass one, so this is not part of its result.
    """


def _stop(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ShotSuperseded()


def assemble_shot(
    shot_dir: Path,
    *,
    budget: int = DEFAULT_BUDGET,
    preset: str | None = None,
    shares: Mapping[str, int] | None = None,
    results_dir: Path | None = None,
    shot_id: str | None = None,
    allow_pose_download: bool = True,
    timings: dict[str, float] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> SwingEvidencePackage:
    """Read one raw shot and write a versioned package beside it, not over it.

    `shot_id` defaults to the directory name, which is what the research
    command uses. `allow_pose_download` stays on for that command. A live
    caller turns it off so a missing pose file is unresolved evidence rather
    than a download. `timings` receives seconds for each stage when given.
    """

    require_opencv4()
    shot_id = shot_id or shot_dir.name
    clock = timings if timings is not None else {}
    started = time.perf_counter()
    loaded: dict[str, tuple[TimelineFrame, ...]] = {}
    motion_grays: dict[str, tuple[np.ndarray, ...]] = {}
    impact_grays: dict[str, tuple[np.ndarray, ...]] = {}
    rgb_by_camera: dict[str, dict[Path, np.ndarray]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    duplicates: dict[str, int] = {}
    warnings: list[str] = []
    errors: list[str] = []
    _stop(cancelled)
    load_started = time.perf_counter()
    timelines: dict[str, tuple[TimelineFrame, ...]] = {}
    for camera_id in CAMERAS:
        _stop(cancelled)
        try:
            timelines[camera_id] = tuple(load_timeline(shot_dir, camera_id))
        except ValueError as exc:
            warnings.append(str(exc))
    clock["timeline_seconds"] = time.perf_counter() - load_started
    pose_color = _pose_available(allow_pose_download=allow_pose_download)
    decode_started = time.perf_counter()
    for camera_id, timeline in timelines.items():
        _stop(cancelled)
        duplicates[camera_id] = duplicate_count(timeline)
        good, motion, impact, colors, bad = _decode_camera(timeline, rgb=pose_color)
        if bad:
            warnings.append(f"{camera_id} rejected {len(bad)} unreadable frames")
        if good:
            loaded[camera_id] = good
            motion_grays[camera_id] = motion
            impact_grays[camera_id] = impact
            if colors:
                rgb_by_camera[camera_id] = colors
            rejected[camera_id] = bad
    clock["decode_seconds"] = time.perf_counter() - decode_started
    requested, missing = resolve_allocation(
        tuple(loaded), budget, preset=preset, shares=shares
    )
    shares_now = dict(requested)
    sampled: dict[str, dict[str, Any]] = {}
    motion_started = time.perf_counter()
    for camera_id, frames in loaded.items():
        _stop(cancelled)
        share = shares_now.get(camera_id, 0)
        if share <= 0:
            continue
        sampled[camera_id] = run_timeline(
            frames,
            budget=share,
            camera_id=camera_id,
            shot_id=shot_id,
            grays=motion_grays[camera_id],
        )
    sampled, shares_now = _give_unused_budget(
        sampled, loaded, motion_grays, shares_now, shot_id
    )
    clock["motion_seconds"] = time.perf_counter() - motion_started
    clock["image_loading_seconds"] = _sampler_time(sampled, "image_loading")
    clock["frame_difference_seconds"] = _sampler_time(sampled, "frame_difference")
    clock["optical_flow_seconds"] = _sampler_time(sampled, "optical_flow")
    clock["sampler_selection_seconds"] = _sampler_time(sampled, "selection")
    _stop(cancelled)
    stages, stage_seconds, stage_notes = _stages(
        shot_dir,
        tuple(loaded),
        shot_id=shot_id,
        allow_pose_download=allow_pose_download,
        cancelled=cancelled,
        rgb_by_camera=rgb_by_camera,
    )
    warnings.extend(stage_notes)
    clock["pose_seconds"] = stage_seconds
    _stop(cancelled)
    impacts, periods, impact_seconds = _impacts(
        shot_id, loaded, sampled, impact_grays
    )
    clock["impact_seconds"] = impact_seconds
    fused = (
        fuse_impact(impacts, periods)
        if impacts
        else _empty_impact(shot_id, "no_frames")
    )
    _stop(cancelled)
    pooled: list[CandidateFrame] = []
    boundary_list: list[StageBoundary] = []
    boundary_cameras: list[str] = []
    for camera_id, result in sampled.items():
        pooled.extend(_candidates(camera_id, result.get("frames") or []))
    for camera_id, stage in stages.items():
        for boundary in stage.boundaries:
            boundary_list.append(boundary)
            boundary_cameras.append(camera_id)
    select_started = time.perf_counter()
    chosen, fallback, choice_warnings = choose_frames(
        pooled,
        per_camera_budget=shares_now,
        boundaries=tuple(boundary_list),
        boundary_cameras=tuple(boundary_cameras),
        impact=fused,
    )
    clock["selection_seconds"] = time.perf_counter() - select_started
    warnings.extend(choice_warnings)
    if not chosen:
        errors.append("no frame could be selected")
    present = tuple(camera_id for camera_id in CAMERAS if camera_id in loaded)
    status = (
        STATUS_FAILED
        if not chosen
        else STATUS_OK
        if len(present) == len(CAMERAS)
        else STATUS_PARTIAL
    )
    if missing:
        warnings.append(
            "a requested camera was missing, so its images moved to the cameras on disk"
        )
    preset_name = "custom" if shares is not None else (preset or DEFAULT_PRESET)
    package_started = time.perf_counter()
    allocation = EvidenceAllocation(
        preset=preset_name,
        budget=budget,
        requested=tuple(sorted(requested.items())),
        applied=tuple(sorted(shares_now.items())),
        missing_cameras=missing,
        note="Image shares are view weights. They are not fault weights.",
    )
    package = SwingEvidencePackage(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=status,
        provenance=Provenance(
            stage="evidence_package",
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
        ),
        cameras=present,
        image_budget=budget,
        selected_frames=chosen,
        stage_estimates=tuple(
            stages[camera_id] for camera_id in present if camera_id in stages
        ),
        impact_estimate=fused,
        synchronization=compare_clocks(loaded),
        fallback=fallback,
        raw_capture_preserved=True,
        allocation=allocation,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )
    clock["package_seconds"] = time.perf_counter() - package_started
    clock["total_seconds"] = time.perf_counter() - started
    if results_dir is not None:
        _write_shot(
            package,
            results_dir / shot_id,
            shot_dir,
            loaded,
            sampled,
            impacts,
            {
                "total_seconds": time.perf_counter() - started,
                "stage_seconds": stage_seconds,
                "impact_seconds": impact_seconds,
                "duplicates": duplicates,
                "rejected_files": {key: list(value) for key, value in rejected.items()},
                "high_motion": _high_motion_coverage(sampled, chosen),
                "per_camera_budget": shares_now,
                "working_set_bytes": _working_set()[0],
                "peak_working_set_bytes": _working_set()[1],
            },
        )
    return package


def _sampler_time(sampled: Mapping[str, Mapping[str, Any]], key: str) -> float:
    """Seconds the kept sampler run already measured. Not a second analysis."""

    total = 0.0
    for result in sampled.values():
        timing = result.get("timing_seconds") or {}
        value = timing.get(key) if isinstance(timing, Mapping) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            total += float(value)
    return total


def _decode_camera(
    timeline: Sequence[TimelineFrame], *, rgb: bool
) -> tuple[
    tuple[TimelineFrame, ...],
    tuple[np.ndarray, ...],
    tuple[np.ndarray, ...],
    dict[Path, np.ndarray],
    tuple[str, ...],
]:
    """Read each file once. Motion gray and impact gray are both kept."""

    kept: list[TimelineFrame] = []
    motion: list[np.ndarray] = []
    impact: list[np.ndarray] = []
    colors: dict[Path, np.ndarray] = {}
    rejected: list[str] = []
    for frame in timeline:
        if not frame.path.is_file() or frame.path.stat().st_size < 32:
            rejected.append(frame.relative_file or str(frame.path))
            continue
        decoded = decode_frame(frame.path, rgb=rgb)
        if decoded is None:
            rejected.append(frame.relative_file or str(frame.path))
            continue
        motion_gray, impact_gray, color = decoded
        kept.append(frame)
        motion.append(motion_gray)
        impact.append(impact_gray)
        if color is not None:
            colors[frame.path] = color
    return tuple(kept), tuple(motion), tuple(impact), colors, tuple(rejected)


def _pose_available(*, allow_pose_download: bool) -> bool:
    """True when a checked model file and MediaPipe are both present.

    `allow_pose_download` does not fetch the model. It remains so a caller
    can still ask a detector failure to propagate.
    """

    del allow_pose_download
    if not active_model_path().is_file():
        return False
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        return False
    return True


def _give_unused_budget(
    sampled: dict[str, dict[str, Any]],
    loaded: Mapping[str, Sequence[TimelineFrame]],
    motion_grays: Mapping[str, Sequence[np.ndarray]],
    shares: dict[str, int],
    shot_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    unused = 0
    receivers = []
    for camera_id, result in sampled.items():
        gap = shares[camera_id] - int(result["selected_count"])
        if gap > 0:
            unused += gap
        else:
            receivers.append(camera_id)
    if unused <= 0 or len(receivers) != 1:
        return sampled, shares
    camera_id = receivers[0]
    shares[camera_id] += unused
    sampled[camera_id] = run_timeline(
        loaded[camera_id],
        budget=shares[camera_id],
        camera_id=camera_id,
        shot_id=shot_id,
        grays=motion_grays[camera_id],
    )
    return sampled, shares


def _stages(
    shot_dir: Path,
    cameras: Sequence[str],
    *,
    shot_id: str,
    allow_pose_download: bool = True,
    cancelled: Callable[[], bool] | None = None,
    rgb_by_camera: Mapping[str, Mapping[Path, np.ndarray]] | None = None,
) -> tuple[dict[str, SwingStageResult], float, tuple[str, ...]]:
    """Pose stages for each camera. A missing model is unresolved, not invented.

    The pose file is never downloaded from here. Install it with
    `scripts/install-pose-model.py`. A live caller sets `allow_pose_download`
    false so a detector failure stays on this shot instead of propagating.
    """

    started = time.perf_counter()
    notes: list[str] = []
    model_path = active_model_path()
    if not model_path.is_file():
        notes.append(
            "pose model is not installed at "
            f"{model_path}; swing stages are unresolved"
        )
        return {}, time.perf_counter() - started, tuple(notes)
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        notes.append("MediaPipe is not installed; swing stages are unresolved")
        return {}, time.perf_counter() - started, tuple(notes)

    def _one(camera_id: str) -> tuple[str, SwingStageResult | None, str | None]:
        _stop(cancelled)
        try:
            camera_frames = load_camera_frames(shot_dir, camera_id)
            colors = (rgb_by_camera or {}).get(camera_id) or {}
            if colors:
                camera_frames = [
                    replace(frame, rgb=colors.get(frame.path)) for frame in camera_frames
                ]
            poses, pose_warnings, _init, _inference = track_poses(camera_frames)
            result = detect_swing_stages(poses, shot_id=shot_id, camera_id=camera_id)
        except ShotSuperseded:
            raise
        except Exception as exc:
            if allow_pose_download:
                raise
            return (
                camera_id,
                None,
                f"{camera_id} stage detection failed ({type(exc).__name__}: {exc}); "
                "its stages are unresolved",
            )
        if pose_warnings:
            result = replace(result, warnings=result.warnings + tuple(pose_warnings))
        return camera_id, result, None

    # The two views do not share a tracker. Running them together is the same
    # detector twice, and the stage boundaries match the one-camera order.
    if len(cameras) > 1:
        with ThreadPoolExecutor(max_workers=len(cameras)) as pool:
            outcomes = list(pool.map(_one, cameras))
    else:
        outcomes = [_one(camera_id) for camera_id in cameras]
    results: dict[str, SwingStageResult] = {}
    for camera_id, result, note in outcomes:
        if note:
            notes.append(note)
        if result is not None:
            results[camera_id] = result
    return results, time.perf_counter() - started, tuple(notes)


def _impacts(
    shot_id: str,
    loaded: Mapping[str, Sequence[TimelineFrame]],
    sampled: Mapping[str, Mapping[str, Any]],
    impact_grays: Mapping[str, Sequence[np.ndarray]],
) -> tuple[list[ImpactEstimate], list[float], float]:
    started = time.perf_counter()
    estimates: list[ImpactEstimate] = []
    periods: list[float] = []
    for camera_id, frames in loaded.items():
        grays = list(impact_grays.get(camera_id, ()))
        kept_ticks = [frame.capture_tick for frame in frames]
        kept_times = [frame.offset_seconds for frame in frames]
        if len(grays) != len(frames):
            grays = []
            kept_ticks = []
            kept_times = []
        useful = 0
        record = (sampled.get(camera_id) or {}).get("useful_sequence") or {}
        start_tick = record.get("start_tick")
        if isinstance(start_tick, int) and start_tick in kept_ticks:
            useful = kept_ticks.index(start_tick)
        estimates.append(
            estimate_ball_motion(
                kept_ticks,
                kept_times,
                grays,
                shot_id=shot_id,
                camera_id=camera_id,
                useful_start=useful,
            )
        )
        deltas = [
            right - left
            for left, right in zip(kept_times, kept_times[1:], strict=False)
            if right > left
        ]
        periods.append(sorted(deltas)[len(deltas) // 2] if deltas else 0.1)
    return estimates, periods, time.perf_counter() - started


def _empty_impact(shot_id: str, reason: str) -> ImpactEstimate:
    return ImpactEstimate(
        schema_version=SCHEMA_VERSION,
        shot_id=shot_id,
        status=IMPACT_UNRESOLVED,
        provenance=Provenance(
            stage="impact_estimation_fused",
            producer="impact_estimator",
            producer_version="1",
        ),
        classification=UNSUPPORTED_ESTIMATE,
        reason=reason,
        confidence=None,
        confidence_scale=CONFIDENCE_SCALE,
        method=METHOD,
        camera_id=None,
        contact_interval_ticks=None,
        contact_interval_seconds=None,
    )


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
    reader = windll.psapi.GetProcessMemoryInfo
    reader.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    reader.restype = wintypes.BOOL
    if not reader(
        windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None, None
    return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)


def _write_shot(
    package: SwingEvidencePackage,
    directory: Path,
    shot_dir: Path,
    loaded: Mapping[str, Sequence[TimelineFrame]],
    sampled: Mapping[str, Mapping[str, Any]],
    impacts: Sequence[ImpactEstimate],
    notes: Mapping[str, Any],
) -> None:
    if (
        shot_dir.resolve() in directory.resolve().parents
        or directory.resolve() == shot_dir.resolve()
    ):
        raise ValueError("evidence artifacts must not be written into the raw capture")
    directory.mkdir(parents=True, exist_ok=True)
    stamped = _stamp(package)
    _write_json(directory / "evidence_package.json", to_plain(stamped))
    _write_json(
        directory / "impact_analysis.json",
        {
            "schema_version": SCHEMA_VERSION,
            "shot_id": package.shot_id,
            "fused": to_plain(stamped.impact_estimate),
            "cameras": [to_plain(item) for item in impacts],
            "synchronization": to_plain(stamped.synchronization),
            "protee_shot_detected": {"used": False, "reason": _PROTEE_NOTE},
            "performance": notes,
        },
    )
    _write_impact_sheet(directory / "impact_review_sheet.jpg", impacts, loaded)
    _write_evidence_sheet(directory / "evidence_review_sheet.jpg", shot_dir, stamped)
    _write_timeline(directory / "evidence_timeline.png", stamped, sampled)


def _stamp(package: SwingEvidencePackage) -> SwingEvidencePackage:
    created = datetime.now(UTC).isoformat()
    return replace(package, provenance=replace(package.provenance, created_at=created))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _open_copy(shot_dir: Path, relative: str) -> Image.Image | None:
    path = (shot_dir / relative).resolve()
    if shot_dir.resolve() not in path.parents or not path.is_file():
        return None
    with Image.open(path) as image:
        return image.convert("RGB")


def _thumb(image: Image.Image, width: int) -> Image.Image:
    height = max(1, int(round(image.height * (width / image.width))))
    return image.resize((width, height), Image.Resampling.BILINEAR)


def _write_impact_sheet(
    path: Path,
    impacts: Sequence[ImpactEstimate],
    loaded: Mapping[str, Sequence[TimelineFrame]],
) -> None:
    cells: list[tuple[str, Image.Image]] = []
    for estimate in impacts:
        camera_id = estimate.camera_id or ""
        shown = list(estimate.supporting_ticks)
        label = f"{camera_id} {estimate.status}"
        if not shown:
            blank = Image.new("RGB", (220, 140), (36, 40, 48))
            ImageDraw.Draw(blank).multiline_text(
                (12, 40),
                f"{camera_id}\n{estimate.status}\n{estimate.reason}",
                fill=(230, 230, 230),
                font=_font(16),
            )
            cells.append((estimate.reason, blank))
            continue
        frames = loaded.get(camera_id, ())
        by_tick = {frame.capture_tick: frame for frame in frames}
        for tick in shown[:8]:
            frame = by_tick.get(tick)
            if frame is None:
                continue
            with Image.open(frame.path) as source:
                image = source.convert("RGB")
            if estimate.normalized_ball is not None:
                draw = ImageDraw.Draw(image)
                x = estimate.normalized_ball[0] * image.width
                y = estimate.normalized_ball[1] * image.height
                draw.ellipse(
                    (x - 12, y - 12, x + 12, y + 12), outline=(255, 64, 64), width=3
                )
            cells.append((f"{label} t{tick}", _thumb(image, 220)))
    _save_grid(path, cells, columns=4, empty="impact unresolved")


def _write_evidence_sheet(
    path: Path, shot_dir: Path, package: SwingEvidencePackage
) -> None:
    cells: list[tuple[str, Image.Image]] = []
    for frame in package.selected_frames:
        image = _open_copy(shot_dir, frame.reference.relative_file)
        if image is None:
            continue
        short = "DTL" if frame.reference.camera_id == "swing_behind" else "FO"
        stage = frame.stage or "-"
        caption = (
            f"{short} {frame.reference.capture_tick} "
            f"{frame.reference.timestamp_seconds:.2f}s\n"
            f"{frame.selection_reason} {stage}"
        )
        cells.append((caption, _thumb(image, 200)))
    title = (
        f"{package.fallback} budget {package.image_budget} "
        f"impact {package.impact_estimate.status}"
    )
    _save_grid(path, cells, columns=6, empty="no frames", banner=title)


def _save_grid(
    path: Path,
    cells: Sequence[tuple[str, Image.Image]],
    *,
    columns: int,
    empty: str,
    banner: str = "",
) -> None:
    if not cells:
        canvas = Image.new("RGB", (640, 80), (245, 245, 245))
        ImageDraw.Draw(canvas).text((16, 30), empty, fill=(0, 0, 0), font=_font(18))
        canvas.save(path, quality=85)
        return
    font = _font(12)
    label_h = 44
    rows = (len(cells) + columns - 1) // columns
    cell_w = max(image.width for _caption, image in cells)
    cell_h = max(image.height for _caption, image in cells)
    banner_h = 28 if banner else 0
    canvas = Image.new(
        "RGB",
        (columns * cell_w, banner_h + rows * (cell_h + label_h)),
        (250, 250, 250),
    )
    draw = ImageDraw.Draw(canvas)
    if banner:
        draw.text((8, 6), banner[:180], fill=(0, 0, 0), font=_font(16))
    for index, (caption, image) in enumerate(cells):
        column = index % columns
        row = index // columns
        x = column * cell_w
        y = banner_h + row * (cell_h + label_h)
        canvas.paste(image, (x, y))
        draw.multiline_text((x + 2, y + cell_h + 2), caption, fill=(0, 0, 0), font=font)
    canvas.save(path, quality=85)


def _write_timeline(
    path: Path,
    package: SwingEvidencePackage,
    sampled: Mapping[str, Mapping[str, Any]],
) -> None:
    del sampled
    width, height = 1200, 360
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    times = [frame.reference.timestamp_seconds for frame in package.selected_frames]
    stage_times = [
        boundary.time_interval_seconds
        for stage in package.stage_estimates
        for boundary in stage.boundaries
    ]
    span_values = list(times)
    for start, end in stage_times:
        span_values.extend((start, end))
    impact = package.impact_estimate.contact_interval_seconds
    if impact is not None:
        span_values.extend(impact)
    if not span_values:
        draw.text((20, 20), "no timeline", fill=(0, 0, 0), font=_font(18))
        canvas.save(path)
        return
    start = min(span_values)
    end = max(span_values)
    if end <= start:
        end = start + 1.0

    def x_at(value: float) -> int:
        return 60 + int(round((value - start) / (end - start) * (width - 100)))

    colors = {"swing_behind": (30, 90, 180), "swing_side": (180, 90, 30)}
    y = {"swing_behind": 120, "swing_side": 180}
    for camera_id, top in y.items():
        draw.line((60, top, width - 40, top), fill=(180, 180, 180), width=2)
        draw.text((8, top - 8), camera_id, fill=(0, 0, 0), font=_font(12))
    for stage in package.stage_estimates:
        top = y.get(stage.camera_id, 80)
        for boundary in stage.boundaries:
            a, b = boundary.time_interval_seconds
            draw.rectangle(
                (x_at(a), top - 16, x_at(b), top + 16), outline=(80, 140, 80)
            )
    if impact is not None and package.impact_estimate.status == IMPACT_ESTIMATED:
        draw.rectangle(
            (x_at(impact[0]), 40, x_at(max(impact[1], impact[0] + 0.01)), 230),
            outline=(180, 40, 40),
        )
        draw.text(
            (x_at(impact[0]), 20), "ball departure", fill=(180, 40, 40), font=_font(14)
        )
    for frame in package.selected_frames:
        camera_id = frame.reference.camera_id
        draw.ellipse(
            (
                x_at(frame.reference.timestamp_seconds) - 4,
                y.get(camera_id, 240) - 4,
                x_at(frame.reference.timestamp_seconds) + 4,
                y.get(camera_id, 240) + 4,
            ),
            fill=colors.get(camera_id, (0, 0, 0)),
        )
    draw.text(
        (60, 280),
        f"{package.fallback}  {package.impact_estimate.status}  "
        f"{package.impact_estimate.classification}",
        fill=(0, 0, 0),
        font=_font(16),
    )
    unresolved = [
        stage.unresolved_stages
        for stage in package.stage_estimates
        if stage.unresolved_stages
    ]
    if unresolved:
        draw.text(
            (60, 310),
            "unresolved stages recorded on the stage estimate",
            fill=(80, 80, 80),
            font=_font(14),
        )
    canvas.save(path)


def _print_summary(package: SwingEvidencePackage) -> None:
    counts: dict[str, int] = {}
    for frame in package.selected_frames:
        counts[frame.reference.camera_id] = counts.get(frame.reference.camera_id, 0) + 1
    impact = package.impact_estimate
    interval = impact.contact_interval_seconds
    print(
        f"{package.shot_id} status={package.status} frames={len(package.selected_frames)} "
        f"cameras={counts} fallback={package.fallback} "
        f"impact={impact.status} interval={interval}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build swing evidence packages.")
    parser.add_argument("shot_id", nargs="+")
    parser.add_argument("--shots-root", type=Path, default=_SHOTS)
    parser.add_argument("--results-root", type=Path, default=_RESULTS)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument(
        "--preset", default=DEFAULT_PRESET, choices=sorted(ALLOCATION_PRESETS)
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.budget <= 0:
        raise SystemExit("budget must be positive")
    failed = 0
    for shot_id in args.shot_id:
        shot_dir = args.shots_root / shot_id
        package = assemble_shot(
            shot_dir,
            budget=args.budget,
            preset=args.preset,
            results_dir=args.results_root,
        )
        _print_summary(package)
        if package.status == STATUS_FAILED:
            failed += 1
    print(args.results_root)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
