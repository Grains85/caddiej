"""Offline: measure the real diagnostic frames. Reads only; prints numbers.

For every shot and body camera, over the replay stills (from the detected
replay start on):

* the motion envelope -- the bounding box of pixels whose brightness range
  across the replay exceeds a threshold, ignoring ProTee's player controls
  along the bottom -- which is how much of each frame is dead background;
* per-still sharpness (variance of a Laplacian) inside that envelope, and a
  per-still motion energy (mean absolute difference from the previous still),
  which is the cheap, deterministic, no-pose signal a sampler could use.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\analyse_frames.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "swing_shots"
SCALE = 4  # analyse at 1/4 resolution: plenty for envelopes and energy
CONTROLS_FRACTION = 0.06  # ProTee's progress bar and buttons, bottom of frame
RANGE_THRESHOLD = 40  # grey levels of max-min across the replay


def grey(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        small = image.convert("L").reduce(SCALE)
        return np.asarray(small, dtype=np.float32)


def laplacian_var(a: np.ndarray) -> float:
    lap = -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    return float(lap.var())


def replay_frames(shot: Path, camera: dict) -> list[dict]:
    return [
        f
        for f in camera["frames"]
        if f["offset_from_replay_start_seconds"] is not None
        and f["offset_from_replay_start_seconds"] >= -1e-6
    ]


PIXEL_CHANGE = 30  # grey levels: well above sensor/JPEG noise
OCCUPANCY = 0.04  # a pixel is "in the motion envelope" if it changed in >=4% of steps


def motion(stack: np.ndarray, offsets: list[float], usable_h: int) -> tuple[np.ndarray, list[float]]:
    """Per-pixel share of steps it changed in, and changed-pixel fraction per second."""

    diffs = np.abs(np.diff(stack[:, :usable_h], axis=0)) > PIXEL_CHANGE
    occupancy = diffs.mean(axis=0)
    rate = [0.0]
    for i in range(1, len(stack)):
        dt = max(1e-3, offsets[i] - offsets[i - 1])
        rate.append(float(diffs[i - 1].mean()) / dt)
    return occupancy, rate


def main() -> None:
    summary: dict[str, object] = {}
    for shot in sorted(p for p in SHOTS.iterdir() if p.is_dir()):
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        print("=" * 90)
        print(shot.name)
        for camera in meta["diagnostic_timeline"]["cameras"]:
            frames = replay_frames(shot, camera)
            stack = np.stack([grey(shot / f["file"]) for f in frames])
            h, w = stack.shape[1:]
            usable_h = int(h * (1 - CONTROLS_FRACTION))
            offsets = [f["offset_from_replay_start_seconds"] for f in frames]
            occupancy, rate = motion(stack, offsets, usable_h)
            mask = occupancy >= OCCUPANCY
            maps = Path(__file__).with_name("output") / "motion_maps"
            maps.mkdir(parents=True, exist_ok=True)
            Image.fromarray((np.clip(occupancy * 4, 0, 1) * 255).astype(np.uint8)).save(
                maps / f"{shot.name}_{camera['camera_id']}.png"
            )
            rows = np.where(mask.mean(axis=1) > 0.005)[0]
            cols = np.where(mask.mean(axis=0) > 0.005)[0]
            if len(rows) and len(cols):
                y0, y1, x0, x1 = rows[0], rows[-1], cols[0], cols[-1]
            else:
                y0, y1, x0, x1 = 0, h - 1, 0, w - 1
            native = (frames[0]["width"], frames[0]["height"])
            box = [int(x0 * SCALE), int(y0 * SCALE), int((x1 + 1) * SCALE), int((y1 + 1) * SCALE)]
            bw, bh = box[2] - box[0], box[3] - box[1]
            print(
                f"-- {camera['camera_id']} native={native[0]}x{native[1]} replay_stills={len(frames)}"
                f" motion_box(x0,y0,x1,y1)={box} size={bw}x{bh}"
                f" area={bw * bh / (native[0] * native[1]):.0%}"
            )
            sub = stack[:, y0 : y1 + 1, x0 : x1 + 1]
            energy = [0.0] + [float(np.abs(sub[i] - sub[i - 1]).mean()) for i in range(1, len(sub))]
            sharp = [laplacian_var(s) for s in sub]
            rows_out = []
            for f, e, s, r in zip(frames, energy, sharp, rate, strict=True):
                rows_out.append(
                    f"{f['capture_index']:02d} {f['offset_from_replay_start_seconds']:+.3f}"
                    f" M={r:5.2f} S={s:6.1f}{' *' + (f.get('selected_phase') or '')[:5] if f['selected'] else ''}"
                )
            for i in range(0, len(rows_out), 3):
                print("   " + " | ".join(rows_out[i : i + 3]))
            summary[f"{shot.name}/{camera['camera_id']}"] = {
                "native": native,
                "motion_box": box,
                "energy": [round(e, 2) for e in energy],
                "motion_rate": [round(r, 3) for r in rate],
                "offsets": offsets,
                "capture_index": [f["capture_index"] for f in frames],
            }
    out = Path(__file__).with_name("output") / "frame_analysis.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
