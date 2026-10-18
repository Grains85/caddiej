"""Time the evidence pipeline on a saved swing. No model call.

    python scripts/benchmark-evidence.py
    python scripts/benchmark-evidence.py --runs 3 --shot 20261008T191545-2-09132997

The first block is process startup: Matplotlib's font cache, MediaPipe, and
one pose-landmarker load that is then closed. Later runs in the same process
are the warm pipeline. A second process, after the font cache exists, is the
warm initialization. This command does not call OpenAI.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from caddiej.swing_evidence.assemble import assemble_shot  # noqa: E402
from caddiej.swing_evidence.motion import load_timeline  # noqa: E402
from caddiej.swing_evidence.ready import ensure_evidence_ready  # noqa: E402

DEFAULT_ROOT = REPO / "research" / "temporal_evidence" / "fresh_v2" / "shots"
DEFAULT_SHOT = "20261008T191545-2-09132997"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark swing evidence. No GPT call."
    )
    parser.add_argument("--shots-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--shot", default=DEFAULT_SHOT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--budget", type=int, default=24)
    parser.add_argument("--preset", default="dtl_primary")
    args = parser.parse_args()
    if args.runs < 1:
        print("runs must be at least 1", file=sys.stderr)
        return 1
    shot_dir = args.shots_root / args.shot
    if not (shot_dir / "metadata.json").is_file():
        print(f"saved swing not found: {shot_dir}", file=sys.stderr)
        return 1

    started = time.perf_counter()
    state = ensure_evidence_ready()
    elapsed = time.perf_counter() - started
    print(
        f"initialization status={state.status} "
        f"seconds={elapsed:.3f} detail={state.detail}"
    )
    if state.elapsed_seconds is not None:
        print(f"initialization recorded seconds={state.elapsed_seconds:.3f}")
    if state.status == "unavailable":
        return 1

    timeline_end: dict[str, float] = {}
    for camera_id in ("swing_behind", "swing_side"):
        frames = load_timeline(shot_dir, camera_id)
        if frames:
            timeline_end[camera_id] = max(frame.offset_seconds for frame in frames)
            print(
                f"timeline {camera_id} frames={len(frames)} last={timeline_end[camera_id]:.4f}"
            )

    for index in range(args.runs):
        timings: dict[str, float] = {}
        wall_started = time.perf_counter()
        package = assemble_shot(
            shot_dir,
            budget=args.budget,
            preset=args.preset,
            timings=timings,
            allow_pose_download=False,
        )
        wall = time.perf_counter() - wall_started
        counts: dict[str, int] = {}
        last_selected: dict[str, float] = {}
        for frame in package.selected_frames:
            camera_id = frame.reference.camera_id
            counts[camera_id] = counts.get(camera_id, 0) + 1
            stamp = frame.reference.timestamp_seconds
            last_selected[camera_id] = max(last_selected.get(camera_id, stamp), stamp)
        print(
            f"run {index + 1} wall={wall:.3f}s status={package.status} "
            f"frames={len(package.selected_frames)} counts={counts} "
            f"fallback={package.fallback} impact={package.impact_estimate.status} "
            f"confidence={package.impact_estimate.confidence}"
        )
        for key in (
            "timeline_seconds",
            "decode_seconds",
            "motion_seconds",
            "image_loading_seconds",
            "frame_difference_seconds",
            "optical_flow_seconds",
            "sampler_selection_seconds",
            "pose_seconds",
            "impact_seconds",
            "selection_seconds",
            "package_seconds",
            "total_seconds",
        ):
            if key in timings:
                print(f"  {key}={timings[key]:.4f}")
        for stage in package.stage_estimates:
            print(
                f"  stage {stage.camera_id} status={stage.status} "
                f"unresolved={list(stage.unresolved_stages)}"
            )
        for camera_id, end in timeline_end.items():
            selected_end = last_selected.get(camera_id)
            print(
                f"  {camera_id} timeline_end={end:.4f} "
                f"selected_end={None if selected_end is None else round(selected_end, 4)}"
            )
        if package.errors:
            print(f"  errors={list(package.errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
