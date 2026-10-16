"""Offline: does a temporal sampler still hold P1-P9 for very different golfers?

No images can be invented, so this works on timelines. A golfer archetype is a
set of phase times relative to impact. ProTee's clip is a fixed 3.00 s cut
from its camera buffer a near-constant time after the strike trigger, so
impact sits ~2.0 s after the replay start whatever the tempo (the impact
stills are at +1.986 / +1.983 / +1.964 s on the three 20 fps swings of
2026-10-08; see replay_anchor.py and POC_REPORT.md for what is and is not
proven). The
capture cadence is the real one from shot 4 (10 fps, then 20 fps from about
+1.43 s), optionally extended to the end of ProTee's ~3.05 s replay at the
bay's 4.5 s default window. Samplers see only timestamps.

A position counts as held when a kept still is within its tolerance of the
instant (or inside its interval, for address, top-with-pause and finish).

Also prints the layout's cell size for every cells-per-camera count, so the
frame-count recommendation can see what each extra cell costs.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\generalisation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from caddiej.domain.cues import PanelView
from caddiej.swing_capture.sheet import CellKind, SheetCell, layout_sheet

ROOT = Path(__file__).resolve().parents[2]
SHOT = ROOT / "media" / "swing_shots" / "20261008T092949-4-d05a29c4"
IMPACT = 2.00
CALIBRATED = (0.00, 0.60, 0.88, 1.64, 1.90, 1.95, 2.00, 2.08)  # today's DTL aims


@dataclass(frozen=True)
class Golfer:
    name: str
    backswing: float  # takeaway start -> top reached
    pause: float  # held at the top
    downswing: float  # top left -> impact
    finish: float  # impact -> finish reached
    p3: bool = True  # lead arm reaches parallel in the backswing


GOLFERS = (
    Golfer("calibration golfer (measured)", 0.95, 0.50, 0.30, 0.45),
    Golfer("1 fast backswing", 0.65, 0.05, 0.25, 0.55),
    Golfer("2 very slow backswing", 1.40, 0.10, 0.33, 0.60),
    Golfer("3 deliberate pause at top", 0.90, 0.60, 0.30, 0.55),
    Golfer("4 short backswing", 0.60, 0.05, 0.24, 0.45),
    Golfer("5 long backswing", 1.10, 0.10, 0.30, 0.65),
    Golfer("6 very fast transition", 0.90, 0.00, 0.20, 0.55),
    Golfer("7 beginner, slow, cast", 1.20, 0.20, 0.45, 0.50),
    Golfer("8 partial swing (pitch)", 0.55, 0.00, 0.30, 0.30, p3=False),
    Golfer("9 driver", 0.95, 0.10, 0.30, 0.75),
    Golfer("10 iron", 0.85, 0.05, 0.28, 0.55),
    Golfer("x extreme: 2.2 s backswing+pause", 1.70, 0.20, 0.35, 0.60),
)


def phases(g: Golfer) -> dict[str, tuple[float, float]]:
    """Interval [lo, hi] in replay seconds within which a still shows the position."""

    top_left = IMPACT - g.downswing
    top_reached = top_left - g.pause
    start = top_reached - g.backswing
    w = g.downswing
    out = {
        "P1": (-9.0, start - 0.05),
        "P2": (start + 0.40 * g.backswing - 0.075, start + 0.40 * g.backswing + 0.075),
        "P4": (top_reached - 0.05, top_left + 0.03),
        "P5": (IMPACT - 0.45 * w - 0.03, IMPACT - 0.45 * w + 0.03),
        "P6": (IMPACT - 0.20 * w - 0.03, IMPACT - 0.20 * w + 0.03),
        "P7": (IMPACT - 0.03, IMPACT + 0.03),
        "P8": (IMPACT + 0.30 * w - 0.04, IMPACT + 0.30 * w + 0.04),
        "P9": (IMPACT + g.finish, IMPACT + g.finish + 0.5),
    }
    if g.p3:
        out["P3"] = (start + 0.65 * g.backswing - 0.075, start + 0.65 * g.backswing + 0.075)
    return out


def timeline(extended: bool) -> list[float]:
    meta = json.loads((SHOT / "metadata.json").read_text(encoding="utf-8"))
    cam = next(c for c in meta["diagnostic_timeline"]["cameras"] if c["camera_id"] == "swing_behind")
    t = [f["offset_from_replay_start_seconds"] for f in cam["frames"]]
    t = [x for x in t if x is not None and x >= -1e-6]
    if extended:
        last = t[-1]
        x = round(last + 0.1, 3)
        while x <= 3.05:
            t.append(x)
            x = round(x + 0.1, 3)
    return t


def nearest(t: list[float], x: float) -> int:
    return min(range(len(t)), key=lambda i: (abs(t[i] - x), i))


def fill_uniform(t: list[float], chosen: set[int], n: int, lo: float, hi: float) -> None:
    """Add stills in [lo, hi] at the centre of the widest time gap until n are chosen."""

    while len(chosen) < n:
        inside = sorted(i for i in chosen if lo <= t[i] <= hi)
        marks = [lo, *[t[i] for i in inside], hi]
        gaps = sorted(((b - a, a, b) for a, b in zip(marks, marks[1:])), reverse=True)
        added = False
        for _size, a, b in gaps:
            candidates = [i for i in range(len(t)) if a < t[i] < b and i not in chosen]
            if candidates:
                chosen.add(min(candidates, key=lambda i: abs(t[i] - (a + b) / 2)))
                added = True
                break
        if not added:
            return


def sampler_uniform(t: list[float], n: int, calibrated: bool) -> set[int]:
    chosen = {0, len(t) - 1}
    if calibrated:
        chosen |= {nearest(t, x) for x in CALIBRATED}
    fill_uniform(t, chosen, n, t[0], t[-1])
    return chosen


def sampler_core(t: list[float], n: int, calibrated: bool, core: tuple[float, float] = (1.70, 2.20)) -> set[int]:
    """Recommended: every still in the impact-anchored core, context by time.

    The core is ProTee replay geometry (impact is time-locked to the replay
    start), not a golfer's tempo. Outside it, context goes to the widest gaps,
    first before the core (address to top), then after it (release to finish),
    in proportion to the time each side spans.
    """

    chosen = {0, len(t) - 1}
    if calibrated:
        chosen |= {nearest(t, x) for x in CALIBRATED}
    in_core = [i for i in range(len(t)) if core[0] <= t[i] <= core[1]]
    room = n - len(chosen | set(in_core))
    if room < 2:
        # Not enough room for context: thin the core symmetrically from its ends.
        keep = n - len(chosen) - 2
        mid = len(in_core) // 2
        in_core = in_core[max(0, mid - keep // 2): mid + (keep - keep // 2)]
    chosen |= set(in_core)
    before = core[0] - t[0]
    after = max(0.0, t[-1] - core[1])
    budget = n - len(chosen)
    take_before = round(budget * before / (before + after)) if before + after else budget
    fill_uniform(t, chosen, len(chosen) + take_before, t[0], core[0])
    fill_uniform(t, chosen, n, core[1], t[-1])
    fill_uniform(t, chosen, n, t[0], t[-1])
    return chosen


def sampler_final(
    t: list[float],
    n: int,
    calibrated: bool = True,
    core: tuple[float, float] = (1.75, 2.15),
    after: int = 2,
) -> set[int]:
    """Core + superset, a fixed small follow-through share, the rest to the backswing."""

    chosen = {0, len(t) - 1}
    if calibrated:
        chosen |= {nearest(t, x) for x in CALIBRATED}
    chosen |= {i for i in range(len(t)) if core[0] <= t[i] <= core[1]}
    tail = sum(1 for i in chosen if t[i] > core[1])
    fill_uniform(t, chosen, len(chosen) + max(0, after - tail), core[1], t[-1])
    fill_uniform(t, chosen, n, t[0], core[0])
    fill_uniform(t, chosen, n, t[0], t[-1])
    return chosen


def held(t: list[float], chosen: set[int], spans: dict[str, tuple[float, float]]) -> list[str]:
    missed = []
    end = t[-1]
    for p, (lo, hi) in sorted(spans.items()):
        if lo > end:
            missed.append(f"{p}(outside window)")
            continue
        if hi < 0:
            missed.append(f"{p}(before replay)")
            continue
        if not any(lo <= t[i] <= hi for i in chosen):
            missed.append(p)
    return missed


def cell_sizes() -> None:
    meta = json.loads((SHOT / "metadata.json").read_text(encoding="utf-8"))
    imp = next(c for c in meta["cameras"] if c["camera_id"] == "impact")["frames"][0]
    av = meta["coach_metrics"]
    print("Cell size by cells per camera (production layout_sheet, 2048 square, All Values full width):")
    for n in range(8, 25, 2):
        cells = []
        for k in range(n):
            cells.append((SheetCell(f"DTL_{k}", "x", Path("x"), CellKind.BODY, "swing_behind", PanelView.BEHIND), 1002, 1480))
        for k in range(n):
            cells.append((SheetCell(f"FO_{k}", "x", Path("x"), CellKind.BODY, "swing_side", PanelView.SIDE), 1272, 1490))
        cells.append((SheetCell("IMPACT", "x", Path("x"), CellKind.PANEL, "impact", PanelView.NONE), imp["width"], imp["height"]))
        cells.append((SheetCell("ALL_VALUES", "x", Path("x"), CellKind.METRICS, "m", PanelView.NONE), av["width"], av["height"]))
        lay = layout_sheet(cells, 2048)
        d = lay.placement("DTL_0").image
        f = lay.placement("FO_0").image
        patches = -(-lay.width // 32) * -(-lay.height // 32)
        print(
            f"  {n:2d}/camera ({2 * n} body)  sheet {lay.width}x{lay.height}  DTL {d.width}x{d.height}"
            f"  FO {f.width}x{f.height}  golfer DTL ~{round(d.height * 0.50)} px  ~{-(-patches * 12 // 10)} img tok"
        )


def main() -> None:
    cell_sizes()
    for extended in (False, True):
        t = timeline(extended)
        print()
        print(
            f"Capture window {'extended to 3.05 s (bay default 4.5 s)' if extended else 'as on the bay (4.0 s): replay +0.00..+2.47'}"
            f" -- {len(t)} replay stills per camera"
        )
        for n in (10, 14, 18):
            for label, fn in (
                ("uniform", lambda tt, nn: sampler_uniform(tt, nn, False)),
                ("uniform+cal", lambda tt, nn: sampler_uniform(tt, nn, True)),
                ("core", lambda tt, nn: sampler_core(tt, nn, False)),
                ("core+cal", lambda tt, nn: sampler_core(tt, nn, True)),
            ):
                total = got = 0
                fails = []
                for g in GOLFERS:
                    spans = phases(g)
                    chosen = fn(t, n)
                    missed = held(t, chosen, spans)
                    sampling_misses = [m for m in missed if "(" not in m]
                    total += len(spans) - (len(missed) - len(sampling_misses))
                    got += len(spans) - len(missed)
                    if missed:
                        fails.append(f"[{g.name.split(' ')[0]}:{','.join(missed)}]")
                print(f"  N={n:2d} {label:<12} held {got:3d}/{total:3d} reachable  {' '.join(fails)}")
        print("  Kept (core+cal, N=18):", [round(t[i], 2) for i in sorted(sampler_core(t, 18, True))])
        for n in (14, 16, 18, 20):
            for core in ((1.75, 2.15), (1.70, 2.20)):
                for after in (2, 3):
                    got = total = 0
                    fails = []
                    for g in GOLFERS:
                        spans = phases(g)
                        missed = held(t, sampler_final(t, n, True, core, after), spans)
                        real = [m for m in missed if "(" not in m]
                        total += len(spans) - (len(missed) - len(real))
                        got += len(spans) - len(missed)
                        if real:
                            fails.append(f"[{g.name.split(' ')[0]}:{','.join(real)}]")
                    print(f"  FINAL N={n} core={core} after={after}: held {got}/{total}  {' '.join(fails)}")
            print("    kept:", [round(t[i], 2) for i in sorted(sampler_final(t, n))])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
