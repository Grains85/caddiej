"""Offline experiment: tempo-independent temporal evidence sheets from real shots.

Nothing here is production code and nothing is sent anywhere. It reads the
diagnostic timelines retained under media/swing_shots/<shot>/diagnostic/,
chooses neutral chronological cells per camera with several samplers, scores
each sampler against labels read by eye off the full-resolution stills, and
draws candidate sheets with the production layout function
(`caddiej.swing_capture.sheet.layout_sheet`, imported read-only).

Outputs under research/temporal_evidence/output/:
  sheets/<shot>_<variant>.jpg     candidate sheets
  sheets/<shot>_<variant>.json    cell -> original provenance table
  crops/<shot>_<variant>_*.png    1:1 crops of the sheet for readability review
  report.json                     dimensions, bytes, cell sizes, token estimates

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\temporal_sheets.py
"""

from __future__ import annotations

import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from caddiej.domain.cues import PanelView
from caddiej.swing_capture.sheet import CellKind, SheetCell, layout_sheet

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "swing_shots"
OUT = Path(__file__).with_name("output")
ANALYSIS = OUT / "frame_analysis.json"

JPEG_QUALITY = 90
MAX_SIDE = 2048
FAST_GAP = 0.075  # a still this close to a neighbour was taken at the fast rate
BLANK_BYTES = 800

# --- ground truth, read by eye off the full-resolution stills -----------------
# Acceptance sets of capture indices: a sampler "covers" a position when it kept
# at least one still in the set. Shots 5 and 6 reuse shot 4's early-swing sets
# (address..top, finish), which their contact sheets agree with to a still.
# P3 includes still 24 (+0.88 s): docs/swing-capture.md measured lead arm
# parallel at 0.74-0.88 s on the full-resolution originals.
_EARLY = {"P1": {15, 16, 17}, "P2": {19, 20, 21}, "P3": {22, 23, 24}, "P4": set(range(28, 37)), "P9": {48, 49, 50, 51}}
TRUTH: dict[str, dict[str, set[int]]] = {
    "20261008T092949-4-d05a29c4/swing_behind": {**_EARLY, "P5": {38, 39}, "P6": {39, 40}, "P7": {41}, "P8": {42, 43}},
    "20261008T092949-4-d05a29c4/swing_side": {**_EARLY, "P4": set(range(30, 36)), "P5": {37, 38, 39}, "P6": {39, 40}, "P7": {41}, "P8": {42, 43}},
    "20261008T093010-5-6669cd32/swing_behind": {**_EARLY, "P5": {38, 39}, "P6": {40}, "P7": {41}, "P8": {42, 43}},
    "20261008T093033-6-819a363f/swing_behind": {**_EARLY, "P5": {37, 38}, "P6": {39}, "P7": {40}, "P8": {42, 43}},
}


@dataclass(frozen=True)
class Still:
    capture_index: int
    rel: float  # seconds from this camera's detected replay start
    burst: float  # seconds from the burst's zero
    file: str
    width: int
    height: int
    sha: str
    bytes: int
    calibrated: str | None  # the phase today's selector aimed this still at, if any


def pool(camera: dict) -> list[Still]:
    """Replay stills, blanks and byte-identical repeats dropped. Deterministic."""

    kept: list[Still] = []
    seen: set[str] = set()
    for f in camera["frames"]:
        rel = f["offset_from_replay_start_seconds"]
        if rel is None or rel < -1e-6 or f["bytes"] < BLANK_BYTES:
            continue
        if f["sha256"] in seen:
            continue
        seen.add(f["sha256"])
        kept.append(
            Still(
                f["capture_index"], rel, f["offset_seconds"], f["file"], f["width"], f["height"],
                f["sha256"], f["bytes"], f.get("selected_phase") if f["selected"] else None,
            )
        )
    return kept


# --- samplers ----------------------------------------------------------------


def _quantile_pick(stills: list[Still], weights: list[float], n: int, must: set[int]) -> list[int]:
    """N stills at equal steps of cumulative weight, `must` always included.

    Positions are the midpoint rule over cumulative weight, so ends are only
    taken when `must` names them. Each target takes the nearest unused still.
    """

    w = np.asarray(weights, dtype=float)
    w = np.where(w <= 0, 1e-6, w)
    cum = np.concatenate([[0.0], np.cumsum(w)])
    centre = (cum[:-1] + cum[1:]) / 2
    chosen = [i for i, s in enumerate(stills) if s.capture_index in must]
    remaining = n - len(chosen)
    if remaining <= 0:
        return sorted(chosen)[:n]
    # Fill the gaps the forced stills leave, by largest weighted gap first.
    while len(chosen) < n:
        marks = sorted(centre[i] for i in chosen)
        bounds = [0.0, *marks, cum[-1]]
        gaps = [(b - a, a, b) for a, b in zip(bounds, bounds[1:])]
        gaps.sort(reverse=True)
        placed = False
        for size, a, b in gaps:
            target = (a + b) / 2
            order = sorted(range(len(stills)), key=lambda i: (abs(centre[i] - target), i))
            for i in order:
                if i not in chosen and a < centre[i] < b:
                    chosen.append(i)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            break
    return sorted(chosen)


def time_weights(stills: list[Still]) -> list[float]:
    """Each still stands for the time half-way to its neighbours."""

    t = [s.rel for s in stills]
    out = []
    for i in range(len(t)):
        left = t[i] - t[i - 1] if i else (t[1] - t[0] if len(t) > 1 else 1)
        right = t[i + 1] - t[i] if i + 1 < len(t) else left
        out.append((left + right) / 2)
    return out


def fast_flags(stills: list[Still]) -> list[bool]:
    t = [s.rel for s in stills]
    flags = []
    for i in range(len(t)):
        gaps = [abs(t[i] - t[j]) for j in (i - 1, i + 1) if 0 <= j < len(t)]
        flags.append(min(gaps) < FAST_GAP if gaps else False)
    return flags


def sample_uniform(stills: list[Still], n: int, must: set[int]) -> list[int]:
    return _quantile_pick(stills, time_weights(stills), n, must)


def sample_variable(stills: list[Still], n: int, must: set[int], rho: float = 2.5) -> list[int]:
    """Denser where the capture itself was denser: a broad window, not a phase."""

    tw = time_weights(stills)
    return _quantile_pick(stills, [w * (rho if f else 1.0) for w, f in zip(tw, fast_flags(stills))], n, must)


def sample_motion(stills: list[Still], n: int, must: set[int], motion: dict[int, float], alpha: float = 0.4) -> list[int]:
    """Half time, half visual change: tempo-independent by construction."""

    tw = np.asarray(time_weights(stills))
    m = np.asarray([motion.get(s.capture_index, 0.0) for s in stills]) * tw
    m = np.convolve(m, [0.25, 0.5, 0.25], mode="same")
    w = alpha * tw / tw.sum() + (1 - alpha) * m / max(m.sum(), 1e-9)
    return _quantile_pick(stills, list(w), n, must)


CORE = (1.75, 2.15)  # ProTee replay geometry: impact ~+2.00 s whatever the tempo
AFTER_CORE = 2


def _widest_gap_fill(stills: list[Still], chosen: set[int], n: int, lo: float, hi: float) -> None:
    while len(chosen) < n:
        inside = sorted(stills[i].rel for i in chosen if lo <= stills[i].rel <= hi)
        marks = [lo, *inside, hi]
        gaps = sorted(((b - a, a, b) for a, b in zip(marks, marks[1:])), reverse=True)
        for _size, a, b in gaps:
            free = [i for i, s in enumerate(stills) if a < s.rel < b and i not in chosen]
            if free:
                chosen.add(min(free, key=lambda i: (abs(stills[i].rel - (a + b) / 2), i)))
                break
        else:
            return


def sample_core(stills: list[Still], n: int, must: set[int]) -> list[int]:
    """Recommended: every still in the impact-anchored core, context by time."""

    chosen = {i for i, s in enumerate(stills) if s.capture_index in must}
    chosen |= {i for i, s in enumerate(stills) if CORE[0] <= s.rel <= CORE[1]}
    tail = sum(1 for i in chosen if stills[i].rel > CORE[1])
    _widest_gap_fill(stills, chosen, len(chosen) + max(0, AFTER_CORE - tail), CORE[1], stills[-1].rel)
    _widest_gap_fill(stills, chosen, n, stills[0].rel, CORE[0])
    _widest_gap_fill(stills, chosen, n, stills[0].rel, stills[-1].rel)
    return sorted(chosen)[:n] if len(chosen) > n else sorted(chosen)


def must_have(stills: list[Still], superset: bool) -> set[int]:
    must = {stills[0].capture_index, stills[-1].capture_index}
    if superset:
        must |= {s.capture_index for s in stills if s.calibrated}
    return must


def coverage(key: str, chosen: list[Still]) -> tuple[int, int, list[str]]:
    truth = TRUTH.get(key)
    if truth is None:
        return 0, 0, []
    have = {s.capture_index for s in chosen}
    missed = [p for p, accept in sorted(truth.items()) if not (have & accept)]
    return len(truth) - len(missed), len(truth), missed


# --- sheet --------------------------------------------------------------------


def _envelope(shot: str, camera_id: str, stills: list[Still], width: int, height: int) -> tuple[int, int, int, int]:
    """Union motion envelope across the replay, robust to speckle, with margin."""

    occ = np.asarray(Image.open(OUT / "motion_maps" / f"{shot}_{camera_id}.png"), dtype=float)
    scale = width / occ.shape[1]
    mass_x, mass_y = occ.sum(axis=0), occ.sum(axis=1)

    def span(mass: np.ndarray, lo: float, hi: float) -> tuple[int, int]:
        c = np.cumsum(mass) / mass.sum()
        return int(np.searchsorted(c, lo)), int(np.searchsorted(c, hi))

    x0, x1 = span(mass_x, 0.01, 0.99)
    y0, y1 = span(mass_y, 0.005, 0.995)
    margin = 0.05
    x0 = max(0, int((x0 - margin * occ.shape[1]) * scale))
    x1 = min(width, int((x1 + margin * occ.shape[1]) * scale))
    y0 = max(0, int((y0 - margin * occ.shape[0]) * scale))
    y1 = min(height, int((y1 + margin * occ.shape[0]) * scale))
    return x0, y0, x1, y1


def compose(shot_dir: Path, variant: str, cameras: dict[str, list[Still]], crop: dict[str, tuple[int, int, int, int]] | None) -> dict:
    meta = json.loads((shot_dir / "metadata.json").read_text(encoding="utf-8"))
    impact = next(c for c in meta["cameras"] if c["camera_id"] == "impact")
    impact_frame = impact["frames"][0]
    metrics = meta["coach_metrics"]
    prefix = {"swing_behind": ("DTL", PanelView.BEHIND), "swing_side": ("FO", PanelView.SIDE)}
    cells: list[tuple[SheetCell, int, int]] = []
    sources: dict[str, tuple[Path, tuple[int, int, int, int] | None]] = {}
    provenance: list[dict] = []
    for camera_id, stills in cameras.items():
        tag, view = prefix[camera_id]
        box = None if crop is None else crop[camera_id]
        for n, s in enumerate(stills, start=1):
            cell_id = f"{tag}_{n:02d}"
            w, h = (s.width, s.height) if box is None else (box[2] - box[0], box[3] - box[1])
            cell = SheetCell(cell_id, f"diag:{camera_id}:{s.capture_index}", Path(s.file), CellKind.BODY, camera_id, view, None, s.rel, w, h)
            cells.append((cell, w, h))
            sources[cell_id] = (shot_dir / s.file, box)
            provenance.append({
                "cell_id": cell_id, "camera_id": camera_id, "view": view.value,
                "capture_index": s.capture_index, "diagnostic_file": s.file, "sha256": s.sha,
                "offset_from_replay_start_seconds": s.rel, "burst_offset_seconds": s.burst,
                "calibrated_aim": s.calibrated, "crop": None if box is None else list(box),
            })
    impact_cell = SheetCell("IMPACT", "impact_01", Path(impact_frame["file"]), CellKind.PANEL, "impact", PanelView.NONE, None, None, impact_frame["width"], impact_frame["height"])
    metrics_cell = SheetCell("ALL_VALUES", "all_values", Path(metrics["file"]), CellKind.METRICS, "coach_metrics", PanelView.NONE, None, None, metrics["width"], metrics["height"])
    cells.append((impact_cell, impact_frame["width"], impact_frame["height"]))
    cells.append((metrics_cell, metrics["width"], metrics["height"]))
    sources["IMPACT"] = (shot_dir / impact_frame["file"], None)
    sources["ALL_VALUES"] = (shot_dir / metrics["file"], None)
    provenance.append({"cell_id": "IMPACT", "frame_id": "impact_01", "file": impact_frame["file"]})
    provenance.append({"cell_id": "ALL_VALUES", "frame_id": "all_values", "file": metrics["file"]})

    layout = layout_sheet(cells, MAX_SIDE)
    canvas = Image.new("RGB", (layout.width, layout.height), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    id_font = ImageFont.load_default(size=22)
    cap_font = ImageFont.load_default(size=18)
    head_font = ImageFont.load_default(size=19)
    bands = {PanelView.BEHIND: (25, 78, 140), PanelView.SIDE: (36, 110, 50)}
    sizes = {}
    for cell, _w, _h in cells:
        place = layout.placement(cell.cell_id)
        path, box = sources[cell.cell_id]
        with Image.open(path) as image:
            picture = image.convert("RGB")
        if box is not None:
            picture = picture.crop(box)
        picture = picture.resize((place.image.width, place.image.height), Image.Resampling.LANCZOS)
        canvas.paste(picture, (place.image.x, place.image.y))
        lb = place.label
        colour = bands.get(cell.view, (110, 70, 20) if cell.kind is CellKind.METRICS else (70, 70, 70))
        draw.rectangle((lb.x, lb.y, lb.x + lb.width - 1, lb.y + lb.height - 1), fill=colour)
        draw.text((lb.x + 5, lb.y + 3), cell.cell_id, fill="white", font=id_font)
        if cell.kind is CellKind.BODY:
            cap = f"+{cell.offset_seconds:.2f}s"
            tw = draw.textlength(cap, font=cap_font)
            draw.text((lb.x + lb.width - tw - 5, lb.y + 6), cap, fill=(230, 230, 230), font=cap_font)
        sizes[cell.cell_id] = (place.image.width, place.image.height, round(place.scale, 3))
    header = (
        "CADDIEJ TEMPORAL SWING EVIDENCE   DTL = down the line   FO = face on   "
        "cells in time order, left to right, top to bottom   +s = seconds from that camera's replay start   "
        "DTL and FO are separate clocks"
    )
    draw.text((layout.header.x + 4, layout.header.y + 8), header, fill=(240, 240, 240), font=head_font)
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=JPEG_QUALITY, subsampling=0)
    data = buffer.getvalue()
    (OUT / "sheets").mkdir(parents=True, exist_ok=True)
    name = f"{shot_dir.name}_{variant}"
    (OUT / "sheets" / f"{name}.jpg").write_bytes(data)
    (OUT / "sheets" / f"{name}.json").write_text(json.dumps(provenance, indent=1), encoding="utf-8")
    _crops(canvas, layout, cells, name)
    body = [v for k, v in sizes.items() if k.startswith(("DTL", "FO"))]
    return {
        "variant": variant, "shot": shot_dir.name, "width": layout.width, "height": layout.height,
        "bytes": len(data), "cells": sizes,
        "dtl_cell": next(v for k, v in sizes.items() if k.startswith("DTL")),
        "fo_cell": next(v for k, v in sizes.items() if k.startswith("FO")),
        "impact_cell": sizes["IMPACT"], "all_values_cell": sizes["ALL_VALUES"],
        "body_cells": len(body),
        "tokens_gpt5mini": patch_tokens(layout.width, layout.height),
    }


def _crops(canvas: Image.Image, layout, cells, name: str) -> None:
    """1:1 crops for review: a down-the-line run, a face-on run, Impact, All Values."""

    (OUT / "crops").mkdir(parents=True, exist_ok=True)
    for group in ("DTL", "FO"):
        boxes = [layout.placement(c.cell_id) for c, _w, _h in cells if c.cell_id.startswith(group)]
        boxes = [b for b in boxes if b is not None]
        mid = len(boxes) // 2
        chosen = boxes[max(0, mid - 3): mid + 3]
        x0 = min(b.label.x for b in chosen)
        y0 = min(b.label.y for b in chosen)
        x1 = max(b.image.x + b.image.width for b in chosen)
        y1 = max(b.image.y + b.image.height for b in chosen)
        canvas.crop((x0, y0, min(x1, x0 + 1100), min(y1, y0 + 900))).save(OUT / "crops" / f"{name}_{group}.png")
    for cell_id in ("IMPACT", "ALL_VALUES"):
        b = layout.placement(cell_id)
        canvas.crop((b.label.x, b.label.y, b.image.x + b.image.width, b.image.y + b.image.height)).save(
            OUT / "crops" / f"{name}_{cell_id}.png"
        )


def patch_tokens(width: int, height: int, multiplier: float = 1.2, fit: int = 2048) -> int:
    """gpt-5-mini as measured in coaching/benchmark.py: fit 2048, 32 px patches, x1.2."""

    shrink = min(1.0, fit / max(width, height))
    patches = math.ceil(width * shrink / 32 - 1e-9) * math.ceil(height * shrink / 32 - 1e-9)
    return math.ceil(patches * multiplier - 1e-9)


# --- run ----------------------------------------------------------------------


def main() -> None:
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    report: dict[str, object] = {"samplers": [], "sheets": []}
    shots = sorted(p for p in SHOTS.iterdir() if p.is_dir())
    pools: dict[str, dict[str, list[Still]]] = {}
    for shot in shots:
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        pools[shot.name] = {c["camera_id"]: pool(c) for c in meta["diagnostic_timeline"]["cameras"]}

    samplers = {
        "uniform": lambda st, n, must, mo: sample_uniform(st, n, must),
        "variable": lambda st, n, must, mo: sample_variable(st, n, must),
        "motion": lambda st, n, must, mo: sample_motion(st, n, must, mo),
        "core": lambda st, n, must, mo: sample_core(st, n, must),
    }
    print("Sampler coverage against by-eye labels (positions covered / labelled):")
    for n in (10, 14, 18):
        for name, fn in samplers.items():
            for superset in (False, True):
                tag = f"{name}{'+cal' if superset else ''}"
                total = got = 0
                misses = []
                for shot, cams in pools.items():
                    for camera_id, stills in cams.items():
                        key = f"{shot}/{camera_id}"
                        series = analysis[key]
                        motion = dict(zip(series["capture_index"], series["motion_rate"]))
                        idx = fn(stills, n, must_have(stills, superset), motion)
                        chosen = [stills[i] for i in idx]
                        g, t, missed = coverage(key, chosen)
                        got += g
                        total += t
                        if missed:
                            misses.append(f"{shot[-8:]}/{camera_id[6:]}:{','.join(missed)}")
                        report["samplers"].append({
                            "n": n, "sampler": tag, "key": key, "kept": [s.capture_index for s in chosen],
                            "covered": g, "labelled": t, "missed": missed,
                        })
                print(f"  N={n:2d} {tag:<13} {got:2d}/{total}  missed: {' '.join(misses) or '-'}")
                if name == "core" and superset:
                    for row in report["samplers"][-len(pools) * 2:]:
                        print(f"        {row['key'][-22:]:<22} kept {row['kept']}")

    # Sheets: the recommended sampler at each size, on every shot; plus
    # comparison arms on shot 4 (the benchmark shot).
    chosen_sampler = "core"
    for shot in shots:
        cams = pools[shot.name]
        for n in (10, 14, 18):
            picked: dict[str, list[Still]] = {}
            for camera_id, stills in cams.items():
                series = analysis[f"{shot.name}/{camera_id}"]
                motion = dict(zip(series["capture_index"], series["motion_rate"]))
                idx = samplers[chosen_sampler](stills, n, must_have(stills, True), motion)
                picked[camera_id] = [stills[i] for i in idx]
            report["sheets"].append(compose(shot, f"T{n * 2}", picked, None))
            if n == 18:
                crop = {
                    cid: _envelope(shot.name, cid, st, st[0].width, st[0].height)
                    for cid, st in cams.items()
                }
                report["sheets"].append(compose(shot, f"T{n * 2}crop", picked, crop))
    print()
    print("Sheets:")
    for s in report["sheets"]:
        print(
            f"  {s['shot'][-10:]} {s['variant']:<9} {s['width']}x{s['height']} {s['bytes'] / 1e6:.2f} MB"
            f"  DTL cell {s['dtl_cell'][0]}x{s['dtl_cell'][1]} (x{s['dtl_cell'][2]})"
            f"  FO cell {s['fo_cell'][0]}x{s['fo_cell'][1]} (x{s['fo_cell'][2]})"
            f"  IMPACT {s['impact_cell'][0]}x{s['impact_cell'][1]}  AV {s['all_values_cell'][0]}x{s['all_values_cell'][1]}"
            f"  ~{s['tokens_gpt5mini']} img tok"
        )
    current = []
    for shot in shots:
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        es = meta["evidence_sheet"]
        current.append((shot.name, es["width"], es["height"], es["bytes"], patch_tokens(es["width"], es["height"])))
    print("Current vx13 sheets:", current)
    report["current"] = current
    (OUT / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
