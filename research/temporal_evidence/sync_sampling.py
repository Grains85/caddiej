"""Offline: synchronized temporal sampling and paired-moment sheet layouts.

No OpenAI calls, no production changes. Reads the retained diagnostic
timelines of every saved shot under media/swing_shots/, audits the current T36
cell timing, builds synchronized candidates (cell number = one replay moment)
and renders them in three layouts for inspection.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\sync_sampling.py

Outputs under research/temporal_evidence/output/sync/:
  audit.json                      current T36 DTL/FO timing per ordinal cell
  sampling.json                   moments chosen per sampler, coverage, gaps
  layouts.json                    cell sizes, golfer pixels, token estimates
  <shot>_<sampler>_<layout>.jpg   candidate sheets
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

sys.path.insert(0, str(Path(__file__).parent))
import temporal_sheets as ts  # noqa: E402  (research helper; imports production read-only)

from caddiej.domain.cues import PanelView  # noqa: E402
from caddiej.swing_capture.sheet import CellKind, SheetCell, layout_sheet  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "sync"
SHEETS = HERE / "output" / "sheets"
SIDE = 2048
N_MOMENTS = 18

# Golfer height as a share of the native frame, read by eye off the tiles of all
# three shots (head to feet at address): DTL ~700 of 1480 px, FO ~825 of 1490 px.
GOLFER_SHARE = {"swing_behind": 0.47, "swing_side": 0.55}

# --- by-eye truth on both cameras (full-resolution tiles, output/sync/tiles_*) ---
# Capture indices. Shot 4 is the POC truth; shots 5 and 6 were labelled for this
# study on both cameras. Every P5-P8 event falls on the same index on both cameras.
TRUTH = {
    "20261008T092949-4-d05a29c4": {
        "swing_behind": {
            "P3": {22, 23, 24},
            "P4": set(range(28, 37)),
            "P5": {38, 39},
            "P6": {39, 40},
            "P7": {41},
            "P8": {42, 43},
        },
        "swing_side": {
            "P3": {22, 23, 24},
            "P4": set(range(30, 36)),
            "P5": {37, 38, 39},
            "P6": {39, 40},
            "P7": {41},
            "P8": {42, 43},
        },
    },
    "20261008T093010-5-6669cd32": {
        "swing_behind": {
            "P3": {23, 24, 25},
            "P4": set(range(28, 37)),
            "P5": {38, 39},
            "P6": {40},
            "P7": {41},
            "P8": {42, 43},
        },
        "swing_side": {
            "P3": {24, 25},
            "P4": set(range(28, 37)),
            "P5": {38, 39},
            "P6": {40},
            "P7": {41},
            "P8": {42, 43},
        },
    },
    "20261008T093033-6-819a363f": {
        "swing_behind": {
            "P3": {23, 24, 25},
            "P4": set(range(28, 36)),
            "P5": {37, 38},
            "P6": {39},
            "P7": {40},
            "P8": {42, 43},
        },
        "swing_side": {
            "P3": {24, 25},
            "P4": set(range(28, 36)),
            "P5": {37, 38},
            "P6": {39},
            "P7": {40},
            "P8": {42, 43},
        },
    },
}
CAMERAS = ("swing_behind", "swing_side")
TAG = {"swing_behind": "DTL", "swing_side": "FO"}


@dataclass(frozen=True)
class Moment:
    index: int  # capture tick; both cameras were requested on it together
    t: float  # replay-relative seconds of the tick
    stills: dict  # camera -> ts.Still, or None when that camera has no distinct still


# --- 1. audit -------------------------------------------------------------------


def audit(shots: list[Path]) -> dict:
    out = {}
    for shot in shots:
        rows = json.loads(
            (SHEETS / f"{shot.name}_T36.json").read_text(encoding="utf-8")
        )
        d = [r for r in rows if r.get("camera_id") == "swing_behind"]
        f = [r for r in rows if r.get("camera_id") == "swing_side"]
        cells = []
        for a, b in zip(d, f, strict=True):
            ta, tb = (
                a["offset_from_replay_start_seconds"],
                b["offset_from_replay_start_seconds"],
            )
            cells.append(
                {
                    "cell": a["cell_id"][-2:],
                    "dtl_capture": a["capture_index"],
                    "dtl_t": round(ta, 3),
                    "fo_capture": b["capture_index"],
                    "fo_t": round(tb, 3),
                    "abs_dt": round(abs(ta - tb), 3),
                    "synchronized": a["capture_index"] == b["capture_index"],
                    "dtl_aim": a["calibrated_aim"],
                    "fo_aim": b["calibrated_aim"],
                }
            )
        out[shot.name] = cells
    return out


def tick_skew(shots: list[Path]) -> dict:
    """Per tick, how far apart the two cameras' requested times are, and the cadence."""

    out = {}
    for shot in shots:
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        cams = {c["camera_id"]: c for c in meta["diagnostic_timeline"]["cameras"]}
        a = {
            f["capture_index"]: f["offset_seconds"]
            for f in cams["swing_behind"]["frames"]
        }
        b = {
            f["capture_index"]: f["offset_seconds"]
            for f in cams["swing_side"]["frames"]
        }
        skew = max(abs(a[k] - b[k]) for k in a)
        t = sorted(
            f["offset_from_replay_start_seconds"]
            for f in cams["swing_behind"]["frames"]
            if f["offset_from_replay_start_seconds"] >= 0
        )
        gaps = np.diff(t)
        cad = cams["swing_behind"]["cadence"]
        out[shot.name] = {
            "max_requested_skew_s": round(skew, 4),
            "request_mean_ms": cad["request_mean_ms"],
            "request_max_ms": cad["request_max_ms"],
            "min_tick_gap_s": round(float(gaps.min()), 4),
            "median_gap_slow_s": round(float(np.median(gaps[gaps > ts.FAST_GAP])), 4),
            "median_gap_fast_s": round(float(np.median(gaps[gaps <= ts.FAST_GAP])), 4),
        }
    return out


# --- 2. synchronized moments -----------------------------------------------------


def moments(shot: Path, pools: dict[str, list]) -> tuple[list[Moment], float]:
    """Every replay tick, with each camera's distinct still on it within tolerance.

    Both cameras are requested on the same tick (one asyncio.gather), so a
    camera's still for moment k is its still of tick k. The tolerance is half
    the smallest measured gap between ticks: anything farther away is nearer a
    different moment, so it is never borrowed. A tick whose still was dropped
    (blank or byte-identical repeat) is unavailable on that camera.
    """

    ticks = sorted({s.capture_index: s.rel for c in CAMERAS for s in pools[c]}.items())
    tolerance = min(b[1] - a[1] for a, b in zip(ticks, ticks[1:])) / 2
    by = {c: {s.capture_index: s for s in pools[c]} for c in CAMERAS}
    out = []
    for index, t in ticks:
        stills = {}
        for c in CAMERAS:
            s = by[c].get(index)
            stills[c] = s if s is not None and abs(s.rel - t) <= tolerance else None
        out.append(Moment(index, t, stills))
    return out, tolerance


def _as_stills(ms: list[Moment]) -> list:
    return [ts.Still(m.index, m.t, 0.0, "", 0, 0, "", 0, None) for m in ms]


def sample_sync(ms: list[Moment], n: int) -> list[Moment]:
    """B: today's core sampler run once on the shared moment line, no per-camera aims."""

    seen = [m for m in ms if any(m.stills.values())]
    st = _as_stills(seen)
    idx = ts.sample_core(st, n, {st[0].capture_index, st[-1].capture_index})
    return [seen[i] for i in idx]


def joint_motion(shot: str, ms: list[Moment], analysis: dict) -> dict[int, float]:
    """Per tick, the mean over cameras of motion rate normalised by that camera's median."""

    out: dict[int, list[float]] = {}
    for c in CAMERAS:
        s = analysis[f"{shot}/{c}"]
        rate = dict(zip(s["capture_index"], s["motion_rate"]))
        med = float(np.median([v for v in rate.values() if v > 0])) or 1.0
        for k, v in rate.items():
            out.setdefault(k, []).append(v / med)
    return {k: float(np.mean(v)) for k, v in out.items()}


def sample_realloc(
    ms: list[Moment], n: int, motion: dict[int, float], alpha: float = 0.4
) -> list[Moment]:
    """C: B's core and tail untouched; the pre-core budget spent by time and visual change.

    The core window is ProTee replay geometry (impact ~+2.0 s whatever the
    tempo). Before it, each candidate moment weighs the time it stands for
    times a mix of constant and normalised joint motion, so a still pause at
    the top takes few cells and the moving stretches either side of it take
    more. Nothing here names a swing position.
    """

    b = sample_sync(ms, n)
    lo = ts.CORE[0]
    keep = [m for m in b if m.t >= lo]
    both = [m for m in ms if any(m.stills.values()) and m.t < lo]
    budget = n - len(keep)
    st = _as_stills(both)
    tw = np.asarray(ts.time_weights(st))
    mo = np.asarray([motion.get(m.index, 0.0) for m in both])
    mo = np.convolve(mo, [0.25, 0.5, 0.25], mode="same")
    w = tw * (alpha + (1 - alpha) * mo / max(mo.mean(), 1e-9))
    idx = ts._quantile_pick(st, list(w), budget, {st[0].capture_index})
    return sorted([both[i] for i in idx] + keep, key=lambda m: m.t)


def current(shot: str) -> dict[str, list[int]]:
    rows = json.loads((SHEETS / f"{shot}_T36.json").read_text(encoding="utf-8"))
    return {
        c: [r["capture_index"] for r in rows if r.get("camera_id") == c]
        for c in CAMERAS
    }


def evaluate(shot: str, chosen: dict[str, list[int]], times: dict[int, float]) -> dict:
    out = {}
    for c in CAMERAS:
        have = chosen[c]
        truth = TRUTH[shot][c]
        cov = {p: sorted(set(have) & s) for p, s in truth.items()}
        p3, p5 = truth["P3"], truth["P5"]
        window = [times[i] for i in have if min(p3) - 3 <= i <= max(p5)]
        out[c] = {
            "covered": {p: bool(v) for p, v in cov.items()},
            "cells_in_set": {p: len(v) for p, v in cov.items()},
            "max_gap_P2_to_P5_s": round(max(np.diff(window)), 3)
            if len(window) > 1
            else None,
            "cells_P3_to_P5": sum(1 for i in have if min(p3) <= i <= max(p5)),
        }
    return out


# --- 5. layouts -------------------------------------------------------------------

_MARGIN, _GAP, _LABEL, _HEADER, _BAND_GAP = 8, 8, 30, 36, 16


def _native(shot: Path) -> dict:
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    cams = {
        c["camera_id"]: c["frames"][0] for c in meta["diagnostic_timeline"]["cameras"]
    }
    imp = next(c for c in meta["cameras"] if c["camera_id"] == "impact")["frames"][0]
    m = meta["coach_metrics"]
    return {
        "swing_behind": (cams["swing_behind"]["width"], cams["swing_behind"]["height"]),
        "swing_side": (cams["swing_side"]["width"], cams["swing_side"]["height"]),
        "impact": (imp["width"], imp["height"], imp["file"]),
        "metrics": (m["width"], m["height"], m["file"]),
    }


def plan(layout: str, n: int, native: dict) -> dict:
    """Largest body-image height for n moments, searching columns per row."""

    rd = native["swing_behind"][0] / native["swing_behind"][1]
    rf = native["swing_side"][0] / native["swing_side"][1]
    mw, mh = native["metrics"][:2]
    av_h = (SIDE - 2 * _MARGIN) * mh / mw
    room_h = SIDE - _HEADER - 2 * _MARGIN - _BAND_GAP - av_h - _LABEL
    best = None
    for cols in range(2, 13):
        slots = n + 1  # IMPACT takes the slot after the last moment
        rows = math.ceil(slots / cols)
        if (
            layout == "pairs"
        ):  # one tile per moment: DTL | FO side by side under one label
            per_w = lambda h: h * (rd + rf) + 4  # noqa: E731
            hw = (SIDE - 2 * _MARGIN - (cols - 1) * _GAP - 4 * cols) / (
                cols * (rd + rf)
            )
            hh = (room_h - rows * _LABEL - (rows - 1) * _GAP) / rows
        else:  # "columns": one column per moment, DTL above FO, bands of moments
            per_w = lambda h: h * rf  # noqa: E731
            gutter = 34
            hw = (SIDE - 2 * _MARGIN - gutter - (cols - 1) * _GAP) / (cols * rf)
            hh = (room_h - rows * (_LABEL + 4) - (rows - 1) * _BAND_GAP) / (2 * rows)
        h = math.floor(min(hw, hh))
        if best is None or h > best["h"]:
            best = {
                "layout": layout,
                "moments": n,
                "cols": cols,
                "rows": rows,
                "h": h,
                "tile_w": math.floor(per_w(h)),
            }
    h = best["h"]
    best.update(
        {
            "dtl_cell": [math.floor(h * rd), h],
            "fo_cell": [math.floor(h * rf), h],
            "golfer_px": {
                "DTL": round(h * GOLFER_SHARE["swing_behind"]),
                "FO": round(h * GOLFER_SHARE["swing_side"]),
            },
            "body_images": 2 * n,
            "sheet": [SIDE, SIDE],
            "image_tokens": ts.patch_tokens(SIDE, SIDE),
        }
    )
    return best


def render(shot: Path, ms: list[Moment], layout: str, name: str) -> dict:
    native = _native(shot)
    font_id = ImageFont.load_default(size=22)
    font_cap = ImageFont.load_default(size=18)
    font_head = ImageFont.load_default(size=19)
    font_tag = ImageFont.load_default(size=16)
    canvas = Image.new("RGB", (SIDE, SIDE), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    bands = {"swing_behind": (25, 78, 140), "swing_side": (36, 110, 50)}

    def paste(c: str, s, x: int, y: int, w: int, h: int, tag: str) -> None:
        if s is None:
            draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(90, 90, 90))
            draw.text(
                (x + 6, y + h // 2), "unavailable", fill=(160, 160, 160), font=font_tag
            )
        else:
            with Image.open(shot / s.file) as im:
                canvas.paste(
                    im.convert("RGB").resize((w, h), Image.Resampling.LANCZOS), (x, y)
                )
        tw = draw.textlength(tag, font=font_tag) + 8
        draw.rectangle((x, y, x + tw, y + 20), fill=bands[c])
        draw.text((x + 4, y + 1), tag, fill="white", font=font_tag)

    if layout == "current":
        cells = []
        for c in CAMERAS:
            view = PanelView.BEHIND if c == "swing_behind" else PanelView.SIDE
            w, h = native[c]
            for k, m in enumerate(ms, start=1):
                cells.append(
                    (
                        SheetCell(
                            f"{TAG[c]}_{k:02d}",
                            f"m{m.index}",
                            Path("x"),
                            CellKind.BODY,
                            c,
                            view,
                            None,
                            m.t,
                            w,
                            h,
                        ),
                        w,
                        h,
                    )
                )
        iw, ih, ifile = native["impact"]
        mw, mh, mfile = native["metrics"]
        cells.append(
            (
                SheetCell(
                    "IMPACT",
                    "impact_01",
                    Path(ifile),
                    CellKind.PANEL,
                    "impact",
                    PanelView.NONE,
                    None,
                    None,
                    iw,
                    ih,
                ),
                iw,
                ih,
            )
        )
        cells.append(
            (
                SheetCell(
                    "ALL_VALUES",
                    "all_values",
                    Path(mfile),
                    CellKind.METRICS,
                    "coach_metrics",
                    PanelView.NONE,
                    None,
                    None,
                    mw,
                    mh,
                ),
                mw,
                mh,
            )
        )
        lay = layout_sheet(cells, SIDE)
        canvas = Image.new("RGB", (lay.width, lay.height), (24, 24, 24))
        draw = ImageDraw.Draw(canvas)
        for cell, _w, _h in cells:
            p = lay.placement(cell.cell_id)
            if cell.kind is CellKind.BODY:
                m = ms[int(cell.cell_id[-2:]) - 1]
                s = m.stills[cell.camera_id]
                if s is None:
                    draw.text(
                        (p.image.x + 6, p.image.y + p.image.height // 2),
                        "unavailable",
                        fill=(160, 160, 160),
                        font=font_tag,
                    )
                else:
                    with Image.open(shot / s.file) as im:
                        canvas.paste(
                            im.convert("RGB").resize(
                                (p.image.width, p.image.height),
                                Image.Resampling.LANCZOS,
                            ),
                            (p.image.x, p.image.y),
                        )
                colour = bands[cell.camera_id]
            else:
                src = (
                    native["impact"][2]
                    if cell.cell_id == "IMPACT"
                    else native["metrics"][2]
                )
                with Image.open(shot / src) as im:
                    canvas.paste(
                        im.convert("RGB").resize(
                            (p.image.width, p.image.height), Image.Resampling.LANCZOS
                        ),
                        (p.image.x, p.image.y),
                    )
                colour = (
                    (110, 70, 20) if cell.kind is CellKind.METRICS else (70, 70, 70)
                )
            lb = p.label
            draw.rectangle(
                (lb.x, lb.y, lb.x + lb.width - 1, lb.y + lb.height - 1), fill=colour
            )
            draw.text((lb.x + 5, lb.y + 3), cell.cell_id, fill="white", font=font_id)
            if cell.kind is CellKind.BODY:
                cap = f"+{cell.offset_seconds:.2f}s"
                draw.text(
                    (
                        lb.x + lb.width - draw.textlength(cap, font=font_cap) - 5,
                        lb.y + 6,
                    ),
                    cap,
                    fill=(230, 230, 230),
                    font=font_cap,
                )
        header = (
            "CADDIEJ TEMPORAL SWING EVIDENCE   DTL = down the line   FO = face on   cells in time order   "
            "DTL_nn and FO_nn are the SAME moment from two cameras   +s = seconds from replay start"
        )
        draw.text(
            (lay.header.x + 4, lay.header.y + 8),
            header,
            fill=(240, 240, 240),
            font=font_head,
        )
        dims = {
            "dtl_cell": [
                lay.placement("DTL_01").image.width,
                lay.placement("DTL_01").image.height,
            ],
            "fo_cell": [
                lay.placement("FO_01").image.width,
                lay.placement("FO_01").image.height,
            ],
        }
    else:
        p = plan(layout, len(ms), native)
        h, cols = p["h"], p["cols"]
        dw, fw = p["dtl_cell"][0], p["fo_cell"][0]
        header = (
            "CADDIEJ TEMPORAL SWING EVIDENCE   one numbered moment = one instant of the replay, seen by both cameras   "
            "DTL = down the line   FO = face on   moments in time order, left to right, row by row"
        )
        draw.text((_MARGIN + 4, 8), header, fill=(240, 240, 240), font=font_head)
        y0 = _HEADER + _MARGIN
        slots = [*ms, "IMPACT"]
        for k, m in enumerate(slots):
            r, col = divmod(k, cols)
            if layout == "pairs":
                tile_w = dw + fw + 4
                x = _MARGIN + col * (tile_w + _GAP)
                y = y0 + r * (_LABEL + h + _GAP)
                w_label = tile_w
            else:
                gutter = 34
                x = _MARGIN + gutter + col * (fw + _GAP)
                y = y0 + r * (_LABEL + 4 + 2 * h + _BAND_GAP)
                w_label = fw
                if col == 0:
                    for lab, yy in (("DTL", y + _LABEL), ("FO", y + _LABEL + h + 4)):
                        draw.rectangle(
                            (_MARGIN, yy, _MARGIN + gutter - 4, yy + h - 1),
                            fill=bands[
                                "swing_behind" if lab == "DTL" else "swing_side"
                            ],
                        )
                        draw.text(
                            (_MARGIN + 3, yy + h // 2 - 8),
                            lab,
                            fill="white",
                            font=font_tag,
                        )
            if m == "IMPACT":
                iw, ih, ifile = native["impact"]
                bw = w_label
                bh = round(bw * ih / iw)
                draw.rectangle((x, y, x + bw - 1, y + _LABEL - 1), fill=(70, 70, 70))
                draw.text((x + 5, y + 3), "IMPACT", fill="white", font=font_id)
                with Image.open(shot / ifile) as im:
                    canvas.paste(
                        im.convert("RGB").resize((bw, bh), Image.Resampling.LANCZOS),
                        (x, y + _LABEL),
                    )
                continue
            k1 = k + 1
            draw.rectangle((x, y, x + w_label - 1, y + _LABEL - 1), fill=(64, 64, 64))
            draw.text((x + 5, y + 3), f"{k1:02d}", fill="white", font=font_id)
            cap = f"+{m.t:.2f}s"
            draw.text(
                (x + w_label - draw.textlength(cap, font=font_cap) - 5, y + 6),
                cap,
                fill=(230, 230, 230),
                font=font_cap,
            )
            if layout == "pairs":
                paste(
                    "swing_behind",
                    m.stills["swing_behind"],
                    x,
                    y + _LABEL,
                    dw,
                    h,
                    f"DTL_{k1:02d}",
                )
                paste(
                    "swing_side",
                    m.stills["swing_side"],
                    x + dw + 4,
                    y + _LABEL,
                    fw,
                    h,
                    f"FO_{k1:02d}",
                )
            else:
                paste(
                    "swing_behind",
                    m.stills["swing_behind"],
                    x + (fw - dw) // 2,
                    y + _LABEL,
                    dw,
                    h,
                    f"DTL_{k1:02d}",
                )
                paste(
                    "swing_side",
                    m.stills["swing_side"],
                    x,
                    y + _LABEL + h + 4,
                    fw,
                    h,
                    f"FO_{k1:02d}",
                )
        mw, mh, mfile = native["metrics"]
        aw = SIDE - 2 * _MARGIN
        ah = round(aw * mh / mw)
        ay = SIDE - _MARGIN - ah
        draw.rectangle(
            (_MARGIN, ay - _LABEL, _MARGIN + aw - 1, ay - 1), fill=(110, 70, 20)
        )
        draw.text(
            (_MARGIN + 5, ay - _LABEL + 3), "ALL_VALUES", fill="white", font=font_id
        )
        with Image.open(shot / mfile) as im:
            canvas.paste(
                im.convert("RGB").resize((aw, ah), Image.Resampling.LANCZOS),
                (_MARGIN, ay),
            )
        dims = {"dtl_cell": p["dtl_cell"], "fo_cell": p["fo_cell"]}
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=ts.JPEG_QUALITY, subsampling=0)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.jpg").write_bytes(buffer.getvalue())
    return {
        "name": name,
        "size": list(canvas.size),
        "bytes": len(buffer.getvalue()),
        **dims,
        "image_tokens": ts.patch_tokens(*canvas.size),
    }


# --- run ----------------------------------------------------------------------------


def main() -> None:
    shots = sorted(p for p in ts.SHOTS.iterdir() if p.is_dir())
    analysis = json.loads(ts.ANALYSIS.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)

    aud = audit(shots)
    skew = tick_skew(shots)
    (OUT / "audit.json").write_text(
        json.dumps({"cells": aud, "ticks": skew}, indent=1), encoding="utf-8"
    )
    print("AUDIT current T36 (cell: DTL capture/t | FO capture/t | |dt|):")
    for shot, cells in aud.items():
        bad = [c for c in cells if not c["synchronized"]]
        print(
            f"  {shot}: {len(cells) - len(bad)}/18 synchronized, max |dt| {max(c['abs_dt'] for c in cells):.3f}s"
        )
        for c in cells:
            print(
                f"    {c['cell']}  #{c['dtl_capture']:>2} {c['dtl_t']:6.3f} | #{c['fo_capture']:>2} {c['fo_t']:6.3f} | {c['abs_dt']:.3f}{'' if c['synchronized'] else '  NOT SYNC'}"
            )
    print("TICKS:", json.dumps(skew, indent=1))

    sampling = {}
    renders = []
    for shot in shots:
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        pools = {
            c["camera_id"]: ts.pool(c) for c in meta["diagnostic_timeline"]["cameras"]
        }
        ms, tol = moments(shot, pools)
        times = {m.index: m.t for m in ms}
        unavailable = {c: [m.index for m in ms if m.stills[c] is None] for c in CAMERAS}
        motion = joint_motion(shot.name, ms, analysis)
        picks = {
            "B_sync": sample_sync(ms, N_MOMENTS),
            "C_realloc": sample_realloc(ms, N_MOMENTS, motion),
        }
        entry = {
            "tolerance_s": round(tol, 4),
            "unavailable_ticks": unavailable,
            "A_current": {
                "kept": current(shot.name),
                "eval": evaluate(shot.name, current(shot.name), times),
            },
        }
        for label, chosen in picks.items():
            kept = [m.index for m in chosen]
            seen = {
                c: [m.index for m in chosen if m.stills[c] is not None] for c in CAMERAS
            }
            entry[label] = {"kept": kept, "eval": evaluate(shot.name, seen, times)}
        sampling[shot.name] = entry
        print(
            f"\n{shot.name}  tolerance {tol * 1000:.1f} ms  unavailable {unavailable}"
        )
        for label in ("A_current", "B_sync", "C_realloc"):
            e = entry[label]
            kept = e["kept"]
            shown = (
                kept
                if isinstance(kept, list)
                else f"DTL {kept['swing_behind']} | FO {kept['swing_side']}"
            )
            print(f"  {label:<10} {shown}")
            for c in CAMERAS:
                ev = e["eval"][c]
                print(
                    f"      {TAG[c]:<3} covered {''.join(p[1] if v else '.' for p, v in ev['covered'].items())}"
                    f"  in-set {ev['cells_in_set']}  P3..P5 cells {ev['cells_P3_to_P5']}  max gap P2..P5 {ev['max_gap_P2_to_P5_s']}s"
                )
        for label, chosen in picks.items():
            for layout in ("current", "columns", "pairs"):
                renders.append(
                    render(shot, chosen, layout, f"{shot.name[:17]}_{label}_{layout}")
                )
    (OUT / "sampling.json").write_text(
        json.dumps(sampling, indent=1, default=sorted), encoding="utf-8"
    )

    native = _native(shots[0])
    plans = [
        plan(lay, n, native) for lay in ("columns", "pairs") for n in (18, 21, 24, 27)
    ]
    (OUT / "layouts.json").write_text(
        json.dumps({"plans": plans, "renders": renders}, indent=1), encoding="utf-8"
    )
    print("\nLAYOUT PLANS (body image height h, cells, golfer px):")
    for p in plans:
        print(
            f"  {p['layout']:<8} n={p['moments']:2d} {p['cols']}x{p['rows']}  h={p['h']}  DTL {p['dtl_cell']}  FO {p['fo_cell']}  golfer DTL {p['golfer_px']['DTL']} FO {p['golfer_px']['FO']}  ~{p['image_tokens']} tok"
        )
    print("RENDERS:")
    for r in renders:
        print(
            f"  {r['name']:<45} {r['size']} {r['bytes'] / 1e6:.2f} MB DTL {r['dtl_cell']} FO {r['fo_cell']} ~{r['image_tokens']} tok"
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
