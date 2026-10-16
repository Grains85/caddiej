"""Synchronized T36: 18 shared replay moments, DTL_nn and FO_nn from the same capture tick.

Research tooling (not production). Supersedes the per-camera T36 sampling in
`temporal_sheets.py`, which stays as the frozen generator of the POC 1-4
sheets. Nothing is sent anywhere.

Why one moment line: the diagnostic burst requests both cameras on the same
tick (one `asyncio.gather` per tick in `swing_capture/service.py`), so a
capture index names one instant on both cameras. The old sampler chose each
camera's cells on its own -- forcing in that camera's calibrated aims -- and so
put different ticks under the same cell number before the impact window
(SYNC_REPORT.md section 1).

The flow:
1. Each camera's usable stills: replay stills (at or after the detected
   replay start), blanks and byte-identical repeats dropped (`ts.pool`).
2. One moment per capture tick. A camera's still for that moment is its still
   of the same tick, accepted only if its burst time is within half the
   smallest measured gap between ticks of the tick's time; otherwise that
   camera is unavailable for the moment. Nothing is borrowed or repeated.
3. 18 moments chosen once, for both cameras, by the unchanged core rule
   (`ts.sample_core`: every moment in the replay impact window, two after it,
   the first and last, the rest by widest time gap). No calibrated aim of
   either camera enters the choice.
4. Moment k is printed as DTL_kk and FO_kk, in the current sheet geometry
   (production `layout_sheet`, read-only).

    .venv\\Scripts\\python.exe research\\temporal_evidence\\t36.py [shot-prefix ...]

Writes output/sheets/<shot>_T36S.jpg and <shot>_T36S.json (cell provenance).
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import temporal_sheets as ts  # noqa: E402

from caddiej.domain.cues import PanelView  # noqa: E402
from caddiej.swing_capture.sheet import CellKind, SheetCell, layout_sheet  # noqa: E402

VARIANT = "T36S"
N_MOMENTS = 18
CAMERAS = ("swing_behind", "swing_side")
PREFIX = {
    "swing_behind": ("DTL", PanelView.BEHIND),
    "swing_side": ("FO", PanelView.SIDE),
}
HEADER = (
    "CADDIEJ TEMPORAL SWING EVIDENCE   DTL = down the line   FO = face on   cells in time order   "
    "DTL_nn and FO_nn = the SAME moment, two cameras   +s = seconds from replay start"
)
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Moment:
    number: int
    """1-based position on the sheet: printed as DTL_nn and FO_nn. 0 before choosing."""

    tick: int
    """The capture index both cameras were requested on."""

    t: float
    """Replay-relative seconds of the tick, from the first camera that has it."""

    stills: Mapping[str, ts.Still | None]
    """Camera -> its still of this tick, or None when that camera has none."""


def _cameras(meta: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        c["camera_id"]: c
        for c in meta["diagnostic_timeline"]["cameras"]
        if c["camera_id"] in CAMERAS
    }


def tick_tolerance(meta: Mapping[str, Any]) -> float:
    """Half the smallest gap between two consecutive capture ticks, in seconds."""

    burst: dict[int, float] = {}
    for camera in _cameras(meta).values():
        for frame in camera["frames"]:
            burst.setdefault(frame["capture_index"], frame["offset_seconds"])
    times = sorted(burst.values())
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    return min(gaps) / 2 if gaps else 0.0


def moment_line(meta: Mapping[str, Any]) -> tuple[list[Moment], float]:
    """Every replay tick with at least one usable still, in tick order."""

    cameras = _cameras(meta)
    tolerance = tick_tolerance(meta)
    pools = {
        cid: {s.capture_index: s for s in ts.pool(cameras[cid])} for cid in cameras
    }
    burst: dict[int, float] = {}
    for cid in CAMERAS:
        for frame in cameras.get(cid, {}).get("frames", []):
            burst.setdefault(frame["capture_index"], frame["offset_seconds"])
    line = []
    for tick in sorted(
        set().union(*(p.keys() for p in pools.values())) if pools else ()
    ):
        stills: dict[str, ts.Still | None] = {}
        for cid in CAMERAS:
            still = pools.get(cid, {}).get(tick)
            ok = still is not None and abs(still.burst - burst[tick]) <= tolerance
            stills[cid] = still if ok else None
        present = [s for s in stills.values() if s is not None]
        if present:
            line.append(Moment(0, tick, present[0].rel, stills))
    return line, tolerance


def choose(line: Sequence[Moment], n: int = N_MOMENTS) -> list[Moment]:
    """The sheet's moments: today's core rule, applied once to the shared line."""

    if not line:
        return []
    proxies = [ts.Still(m.tick, m.t, 0.0, "", 0, 0, "", 0, None) for m in line]
    picked = ts.sample_core(proxies, n, {line[0].tick, line[-1].tick})
    return [replace(line[i], number=k) for k, i in enumerate(picked, start=1)]


def plan(meta: Mapping[str, Any], n: int = N_MOMENTS) -> list[Moment]:
    return choose(moment_line(meta)[0], n)


def cell_id(camera_id: str, number: int) -> str:
    return f"{PREFIX[camera_id][0]}_{number:02d}"


def provenance(
    meta: Mapping[str, Any], moments: Sequence[Moment]
) -> list[dict[str, Any]]:
    """Printed cell ID -> the original diagnostic still, per camera then moment.

    The same row shape as the POC sheets' tables, plus the moment and tick. An
    unavailable cell has no `capture_index`, so nothing can read evidence from it.
    """

    rows: list[dict[str, Any]] = []
    for cid in CAMERAS:
        view = PREFIX[cid][1].value
        for m in moments:
            s = m.stills.get(cid)
            if s is None:
                rows.append(
                    {
                        "cell_id": cell_id(cid, m.number),
                        "camera_id": cid,
                        "view": view,
                        "moment": m.number,
                        "tick": m.tick,
                        "available": False,
                    }
                )
                continue
            rows.append(
                {
                    "cell_id": cell_id(cid, m.number),
                    "camera_id": cid,
                    "view": view,
                    "moment": m.number,
                    "tick": m.tick,
                    "available": True,
                    "capture_index": s.capture_index,
                    "diagnostic_file": s.file,
                    "sha256": s.sha,
                    "offset_from_replay_start_seconds": s.rel,
                    "burst_offset_seconds": s.burst,
                    "calibrated_aim": s.calibrated,
                    "crop": None,
                }
            )
    impact = next(c for c in meta["cameras"] if c["camera_id"] == "impact")["frames"][0]
    rows.append({"cell_id": "IMPACT", "frame_id": "impact_01", "file": impact["file"]})
    rows.append(
        {
            "cell_id": "ALL_VALUES",
            "frame_id": "all_values",
            "file": meta["coach_metrics"]["file"],
        }
    )
    return rows


def _native(meta: Mapping[str, Any], camera_id: str) -> tuple[int, int]:
    frame = _cameras(meta)[camera_id]["frames"][0]
    return frame["width"], frame["height"]


def render(
    shot_dir: Path, meta: Mapping[str, Any], moments: Sequence[Moment]
) -> tuple[bytes, dict[str, Any]]:
    """The sheet in the current geometry. An unavailable cell keeps its slot, drawn empty."""

    from PIL import Image, ImageDraw, ImageFont

    cells: list[tuple[SheetCell, int, int]] = []
    files: dict[str, Path | None] = {}
    for cid in CAMERAS:
        view = PREFIX[cid][1]
        native = _native(meta, cid)
        for m in moments:
            s = m.stills.get(cid)
            w, h = (s.width, s.height) if s is not None else native
            name = cell_id(cid, m.number)
            frame = f"diag:{cid}:{m.tick}"
            cells.append(
                (
                    SheetCell(
                        name,
                        frame,
                        Path(s.file if s else ""),
                        CellKind.BODY,
                        cid,
                        view,
                        None,
                        s.rel if s else None,
                        w,
                        h,
                    ),
                    w,
                    h,
                )
            )
            files[name] = None if s is None else shot_dir / s.file
    impact = next(c for c in meta["cameras"] if c["camera_id"] == "impact")["frames"][0]
    metrics = meta["coach_metrics"]
    cells.append(
        (
            SheetCell(
                "IMPACT",
                "impact_01",
                Path(impact["file"]),
                CellKind.PANEL,
                "impact",
                PanelView.NONE,
                None,
                None,
                impact["width"],
                impact["height"],
            ),
            impact["width"],
            impact["height"],
        )
    )
    cells.append(
        (
            SheetCell(
                "ALL_VALUES",
                "all_values",
                Path(metrics["file"]),
                CellKind.METRICS,
                "coach_metrics",
                PanelView.NONE,
                None,
                None,
                metrics["width"],
                metrics["height"],
            ),
            metrics["width"],
            metrics["height"],
        )
    )
    files["IMPACT"] = shot_dir / impact["file"]
    files["ALL_VALUES"] = shot_dir / metrics["file"]

    layout = layout_sheet(cells, ts.MAX_SIDE)
    canvas = Image.new("RGB", (layout.width, layout.height), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    id_font = ImageFont.load_default(size=22)
    cap_font = ImageFont.load_default(size=18)
    head_font = ImageFont.load_default(size=19)
    bands = {PanelView.BEHIND: (25, 78, 140), PanelView.SIDE: (36, 110, 50)}
    sizes: dict[str, tuple[int, int]] = {}
    for cell, _w, _h in cells:
        place = layout.placement(cell.cell_id)
        assert place is not None
        path = files[cell.cell_id]
        if path is not None:
            with Image.open(path) as image:
                picture = image.convert("RGB").resize(
                    (place.image.width, place.image.height), Image.Resampling.LANCZOS
                )
            canvas.paste(picture, (place.image.x, place.image.y))
        else:
            box = place.image
            draw.rectangle(
                (box.x, box.y, box.x + box.width - 1, box.y + box.height - 1),
                outline=(90, 90, 90),
            )
            draw.text(
                (box.x + 8, box.y + box.height // 2 - 9),
                UNAVAILABLE,
                fill=(170, 170, 170),
                font=cap_font,
            )
        lb = place.label
        colour = bands.get(
            cell.view, (110, 70, 20) if cell.kind is CellKind.METRICS else (70, 70, 70)
        )
        draw.rectangle(
            (lb.x, lb.y, lb.x + lb.width - 1, lb.y + lb.height - 1), fill=colour
        )
        draw.text((lb.x + 5, lb.y + 3), cell.cell_id, fill="white", font=id_font)
        if cell.kind is CellKind.BODY:
            cap = UNAVAILABLE if path is None else f"+{cell.offset_seconds:.2f}s"
            tw = draw.textlength(cap, font=cap_font)
            draw.text(
                (lb.x + lb.width - tw - 5, lb.y + 6),
                cap,
                fill=(230, 230, 230),
                font=cap_font,
            )
        sizes[cell.cell_id] = (place.image.width, place.image.height)
    if draw.textlength(HEADER, font=head_font) > layout.header.width - 8:
        raise ValueError("the T36S header does not fit the sheet")
    draw.text(
        (layout.header.x + 4, layout.header.y + 8),
        HEADER,
        fill=(240, 240, 240),
        font=head_font,
    )
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=ts.JPEG_QUALITY, subsampling=0)
    return buffer.getvalue(), {
        "width": layout.width,
        "height": layout.height,
        "cells": sizes,
    }


def build(shot_dir: Path, out: Path = ts.OUT / "sheets") -> dict[str, Any]:
    meta = json.loads((shot_dir / "metadata.json").read_text(encoding="utf-8"))
    moments = plan(meta)
    rows = provenance(meta, moments)
    data, geometry = render(shot_dir, meta, moments)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{shot_dir.name}_{VARIANT}"
    (out / f"{name}.jpg").write_bytes(data)
    (out / f"{name}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return {
        "shot": shot_dir.name,
        "sheet": str(out / f"{name}.jpg"),
        "bytes": len(data),
        "moments": moments,
        "tolerance_s": tick_tolerance(meta),
        **geometry,
    }


def main(argv: Sequence[str]) -> None:
    shots = sorted(
        p for p in ts.SHOTS.iterdir() if p.is_dir() and (p / "metadata.json").exists()
    )
    if argv:
        shots = [
            p for p in shots if any(p.name.startswith(a) or a in p.name for a in argv)
        ]
    for shot in shots:
        result = build(shot)
        print(
            f"{shot.name}: {result['width']}x{result['height']} {result['bytes'] / 1e6:.2f} MB, "
            f"tolerance {result['tolerance_s'] * 1000:.1f} ms -> {result['sheet']}"
        )
        for m in result["moments"]:
            have = " ".join(
                f"{PREFIX[c][0]}#{m.stills[c].capture_index}"
                if m.stills[c]
                else f"{PREFIX[c][0]}:-"
                for c in CAMERAS
            )
            print(f"  {m.number:02d}  tick {m.tick:>2}  +{m.t:.3f}s  {have}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main(sys.argv[1:])
