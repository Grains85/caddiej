"""Offline replay of the saved POC 1 and POC 2 replies through the vx14 hybrid validator.

No OpenAI call; production code imported read-only. The hybrid:

1. LOCATE (orientation) is validated per position, never per camera:
   a. camera and existence;
   b. replay-impact prior: P7 inside R7; P1-P6 before R7's end; P8-P9 after its start;
   c. local order: keep the positions common to EVERY longest time-ordered chain,
      null the rest (a tie nulls both sides; nothing is reordered);
   d. cross-camera: a moving position located on both cameras must agree in replay time
      within TAU; if not, a side outside the other camera's bracket is nulled, and both
      are nulled when neither (or both) can be told apart.
2. A checkpoint's own cell (evidence ownership) is accepted when it lies in its phase's
   candidate region on that camera: strictly between the cells LOCATE kept for the
   neighbouring positions, or on LOCATE's own cell, clipped by the same prior.
3. Accepted entries go through the unchanged `vx.parse_checkpoints` with that phase.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\hybrid_replay.py
"""

from __future__ import annotations

import itertools
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

from caddiej.coaching import vx
from caddiej.coaching.score import score_swing
from caddiej.config import Settings
from caddiej.domain.cues import PanelView
from caddiej.domain.shot import Handedness
from caddiej.domain.swing_phase import PHASE_ORDER

sys.path.insert(0, str(Path(__file__).parent))
import poc2_vx14b as poc2  # noqa: E402
import poc_vx14 as poc1  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "hybrid"
POS = poc1.POSITIONS

# Replay-impact prior: leave-one-out, from shots 5 and 6 only (shot 4 is the test shot).
IMPACT_SEEN = (1.983, 1.964)
R7_HALF = 0.25
R7 = (mean(IMPACT_SEEN) - R7_HALF, mean(IMPACT_SEEN) + R7_HALF)
# Cross-camera agreement, seconds. Holds (P1 address, P4 top, P9 finish) are exempt:
# a pause or a held finish may legitimately be sampled at different ends.
TAU = {"P2": 0.30, "P3": 0.30, "P5": 0.15, "P6": 0.15, "P7": 0.15, "P8": 0.15}
CAMERAS = {"dtl": "swing_behind", "fo": "swing_side"}


def sheet(rows):
    cams = {}
    for key, camera_id in CAMERAS.items():
        r = [row for row in poc1.body(rows) if row["camera_id"] == camera_id]
        cams[key] = {
            "cells": [x["cell_id"] for x in r],
            "t": {x["cell_id"]: x["offset_from_replay_start_seconds"] for x in r},
            "ix": {x["cell_id"]: i for i, x in enumerate(r)},
            "frame": {x["cell_id"]: x["frame_id"] for x in r},
        }
    return cams


def side_ok(p: int, t: float) -> bool:
    if p == 6:
        return R7[0] <= t <= R7[1]
    if p < 6:
        return t < R7[1]
    return t > R7[0]


def longest_chains(seq):
    """All maximum time-ordered subsets of [(pos, cell, t)]; a shared cell only for adjacent positions."""

    best, chains = 0, []
    for n in range(len(seq), 0, -1):
        for combo in itertools.combinations(seq, n):
            ok = all(
                b[2] > a[2] or (a[1] == b[1] and b[0] == a[0] + 1)
                for a, b in zip(combo, combo[1:])
            )
            if ok:
                best = n
                chains.append({c[0] for c in combo})
        if chains:
            break
    return best, chains


def validate_locate(raw, cams):
    """raw: {"dtl": [9 cells|None], "fo": [...]} -> kept, notes."""

    notes = []
    kept = {}
    for key, c in cams.items():
        cells = list(raw[key])
        for p, cell in enumerate(cells):
            if cell is None:
                continue
            if cell not in c["t"]:
                notes.append(f"{key} {POS[p]} {cell}: camera")
                cells[p] = None
            elif not side_ok(p, c["t"][cell]):
                notes.append(f"{key} {POS[p]} {cell} @{c['t'][cell]:.2f}: outside impact prior")
                cells[p] = None
        seq = [(p, cell, c["t"][cell]) for p, cell in enumerate(cells) if cell is not None]
        _n, chains = longest_chains(seq)
        common = set.intersection(*chains) if chains else set()
        for p, cell, _t in seq:
            if p not in common:
                notes.append(f"{key} {POS[p]} {cell}: order")
                cells[p] = None
        kept[key] = cells

    def bracket(key, p):
        c = cams[key]
        before = [c["t"][kept[key][q]] for q in range(p) if kept[key][q] is not None]
        after = [c["t"][kept[key][q]] for q in range(p + 1, 9) if kept[key][q] is not None]
        return (max(before) if before else -1e9, min(after) if after else 1e9)

    brackets = {(k, p): bracket(k, p) for k in cams for p in range(9)}
    drop = []
    for name, tau in TAU.items():
        p = POS.index(name)
        d, f = kept["dtl"][p], kept["fo"][p]
        if d is None or f is None:
            continue
        td, tf = cams["dtl"]["t"][d], cams["fo"]["t"][f]
        if abs(td - tf) <= tau:
            continue
        lo, hi = brackets[("fo", p)]
        d_in = lo - 0.005 <= td <= hi + 0.005
        lo, hi = brackets[("dtl", p)]
        f_in = lo - 0.005 <= tf <= hi + 0.005
        if d_in and not f_in:
            drop.append(("fo", p))
        elif f_in and not d_in:
            drop.append(("dtl", p))
        elif not d_in and not f_in:
            drop += [("dtl", p), ("fo", p)]
        notes.append(f"{name} cameras disagree ({d} {td:.2f} / {f} {tf:.2f}): "
                     + ("both kept" if d_in and f_in else "dropped " + ",".join(k for k, q in drop if q == p)))
    for key, p in drop:
        kept[key][p] = None
    return kept, notes


def region(cams, kept, key, p):
    c = cams[key]
    cells = c["cells"]
    prev = max((c["ix"][kept[key][q]] for q in range(p) if kept[key][q] is not None), default=-1)
    nxt = min((c["ix"][kept[key][q]] for q in range(p + 1, 9) if kept[key][q] is not None), default=len(cells))
    allowed = {cells[i] for i in range(prev + 1, nxt)}
    if kept[key][p] is not None:
        allowed.add(kept[key][p])
    return {x for x in allowed if side_ok(p, c["t"][x])}, kept[key][p] is not None


def judge(spec, cell, cams, kept):
    """-> (phase, anchored) or a rejection reason."""

    key = "dtl" if cell.startswith("DTL_") else "fo" if cell.startswith("FO_") else None
    if key is None:
        return "frame"
    order = [spec.phase, *sorted(spec.phases - {spec.phase}, key=list(PHASE_ORDER).index)]
    for phase in order:
        allowed, anchored = region(cams, kept, key, list(PHASE_ORDER).index(phase))
        if cell in allowed:
            return phase, anchored
    return "outside_region"


def assess(entries, cams, kept):
    """entries: [(spec, item_named_with_cell_id | None | refused-str)] -> accepted, rejected, unanchored."""

    accepted, rejected, unanchored = [], [], 0
    frame_views = {cams[k]["frame"][c]: PanelView.BEHIND if k == "dtl" else PanelView.SIDE
                   for k in cams for c in cams[k]["cells"]}
    body_frames = tuple(frame_views)
    for spec, item in entries:
        cid = spec.checkpoint_id.value
        if item is None:
            continue
        if isinstance(item, str):
            rejected.append((cid, item.removeprefix(vx.REFUSED)))
            continue
        verdict = judge(spec, item["cell"], cams, kept)
        if isinstance(verdict, str):
            rejected.append((cid, verdict))
            continue
        phase, anchored = verdict
        key = "dtl" if item["cell"].startswith("DTL_") else "fo"
        frame = cams[key]["frame"][item["cell"]]
        got, rej = vx.parse_checkpoints({cid: {**item, "cell": frame}}, body_frames=body_frames,
                                        frame_phases={frame: phase}, frame_views=frame_views)
        if rej:
            rejected.append(rej[0])
        else:
            accepted.append(got[0])
            unanchored += not anchored
    return accepted, rejected, unanchored


def grade_assessments(assessments, rows):
    by_frame = {r["frame_id"]: r for r in poc1.body(rows)}
    out = Counter()
    wrong = []
    for a in assessments:
        row = by_frame[a.frame_id]
        sheet_ix = [r["capture_index"] for r in poc1.body(rows) if r["camera_id"] == row["camera_id"]]
        truth = poc1.TRUTH[row["camera_id"]][POS[list(PHASE_ORDER).index(a.phase)]]
        at = sheet_ix.index(row["capture_index"])
        ok = [i for i, ix in enumerate(sheet_ix) if ix in truth]
        verdict = "correct" if row["capture_index"] in truth else (
            "acceptable" if any(abs(at - i) == 1 for i in ok) else "wrong")
        out[verdict] += 1
        if verdict == "wrong":
            wrong.append(f"{a.checkpoint.value}@{row['cell_id']}")
    return dict(out), wrong


def summary(label, assessments, rejected, canon, rows, settings, extra=None):
    metrics = vx.parse_metrics(canon.get("metrics"))
    score = score_swing(metrics, club=None, handedness=Handedness.UNKNOWN, checkpoints=assessments)
    mech = score.mechanics
    concluded = poc1.conclude(canon, assessments, settings)
    graded, wrong = grade_assessments(assessments, rows)
    return {
        "label": label,
        "accepted": len(assessments),
        "rejected": len(rejected),
        "rejection_reasons": dict(Counter(r for _k, r in rejected)),
        "accepted_by_position_truth": graded,
        "accepted_wrong_position": wrong,
        "coverage": None if mech is None else round(mech.coverage, 3),
        "coverage_level": None if mech is None else mech.coverage_level.value,
        "mechanics": None if mech is None else mech.value,
        "phases": None if mech is None else {
            p.phase.value: [p.value, round(p.coverage, 2), f"{p.assessed}/{p.scoreable}"] for p in mech.phases},
        "swing_score": score.value,
        "faults": concluded["faults"],
        "root": concluded["root"],
        "lesson_cue": concluded["lesson_cue"],
        "authority": concluded["authority"],
        "outcome": concluded["outcome"],
        **(extra or {}),
    }


def poc1_entries(payload, cams):
    named = vx.decode_wire({"checkpoints": payload["checkpoints"]}, [])["checkpoints"]
    return [(spec, named.get(spec.checkpoint_id.value)) for specs in vx.WIRE_LISTS.values() for spec in specs]


def poc2_entries(payload, kept):
    out = []
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        p = list(PHASE_ORDER).index(vx.SwingPhase(phase_name))
        cell = kept["dtl"][p] if camera == "dtl" else kept["fo"][p] if camera == "fo" else (kept["dtl"][p] or kept["fo"][p])
        entries = payload["checkpoints"].get(key) or [None] * len(specs)
        for spec, e in zip(specs, entries, strict=True):
            if e is None:
                out.append((spec, None))
            elif cell is None:
                out.append((spec, vx.REFUSED + "not_located"))
            else:
                out.append((spec, {"score": e[0], "conf": e[1] / 100, "cell": cell, "dir": e[2]}))
    return out


# --- output size ---------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z]+|\d{1,3}|[^\sA-Za-z\d]")


def proxy(obj) -> int:
    return len(_TOKEN.findall(json.dumps(obj, separators=(",", ":"))))


def hybrid_payload(payload, located, evidence, entries_3, any_cams):
    """The proposed wire: locate grid, evidence grid, 3-field entries, _any lists led by a camera digit."""

    def num(cell):
        return 0 if cell is None else int(cell.split("_")[1])

    checkpoints = {}
    for key, specs in vx.WIRE_LISTS.items():
        lst = entries_3.get(key, [None] * len(specs))
        checkpoints[key] = ([any_cams.get(key, 1)] + lst) if key.endswith("_any") else lst
    return {
        "locate": [[num(located["dtl"][p]), num(located["fo"][p])] for p in range(9)],
        "evidence": [[num(evidence["dtl"][p]), num(evidence["fo"][p])] for p in range(9)],
        "metrics": payload["metrics"],
        "checkpoints": checkpoints,
        "coach": payload["coach"],
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    rows = poc1.load_cells()
    cams = sheet(rows)
    report = {"R7": [round(R7[0], 3), round(R7[1], 3)], "TAU": TAU}

    # ---- POC 1 ----
    r1 = json.loads((HERE / "output/poc/reply.json").read_text(encoding="utf-8"))
    p1 = r1["payload"]
    old_loc, _ = poc1.validate_locate(p1["locate"], rows)
    canon1, old_acc, old_rej = poc1.read_checkpoints(p1, rows, old_loc)
    kept1, notes1 = validate_locate(p1["locate"], cams)
    acc1, rej1, un1 = assess(poc1_entries(p1, cams), cams, kept1)
    report["poc1"] = {
        "locate_notes": notes1,
        "locate_grade": [(r["camera"], r["position"], r["cell"], r["verdict"]) for r in poc1.grade(kept1, rows)],
        "original": summary("POC1 as validated then", old_acc, old_rej, canon1, rows, settings),
        "hybrid": summary("POC1 under hybrid", acc1, rej1, canon1, rows, settings,
                          {"unanchored_accepts": un1,
                           "rejected_list": rej1}),
    }

    # ---- POC 2 ----
    r2 = json.loads((HERE / "output/poc2/reply.json").read_text(encoding="utf-8"))
    p2 = r2["payload"]
    raw2 = {"dtl": [None if e is None else e[0] for e in p2["locate"]],
            "fo": [None if e is None else e[1] for e in p2["locate"]]}
    loc2, conf2, _n = poc2.read_locate(p2["locate"], rows)
    canon2, old_acc2, old_rej2, _ret = poc2.read_checkpoints(p2, rows, loc2)
    kept2, notes2 = validate_locate(raw2, cams)
    acc2, rej2, un2 = assess(poc2_entries(p2, kept2), cams, kept2)
    report["poc2"] = {
        "locate_notes": notes2,
        "locate_grade": [(r["camera"], r["position"], r["cell"], r["verdict"]) for r in poc1.grade(kept2, rows)],
        "original": summary("POC2 as validated then", old_acc2, old_rej2, canon2, rows, settings),
        "hybrid": summary("POC2 under hybrid", acc2, rej2, canon2, rows, settings,
                          {"rejected_list": rej2}),
    }

    # ---- output size ----
    # Calibrate the proxy on the two real replies, then size each as the hybrid wire.
    ratio = mean([r1["usage"]["output_tokens"] / proxy(p1), r2["usage"]["output_tokens"] / proxy(p2)])
    ev1 = {"dtl": [None] * 9, "fo": [None] * 9}
    any1 = {}
    for key, entries in p1["checkpoints"].items():
        phase_name, camera = key.rsplit("_", 1)
        p = list(PHASE_ORDER).index(vx.SwingPhase(phase_name))
        cells = [e[1] for e in entries if e is not None]
        if not cells:
            continue
        cam = "dtl" if cells[0].startswith("DTL") else "fo"
        if camera == "any":
            any1[key] = 1 if cam == "dtl" else 2
        if ev1[cam][p] is None:
            ev1[cam][p] = cells[0]
    e3_1 = {k: [None if e is None else [e[0], e[2], e[3]] for e in v] for k, v in p1["checkpoints"].items()}
    e3_2 = p2["checkpoints"]
    h1 = hybrid_payload(p1, {k: list(v) for k, v in p1["locate"].items()}, ev1, e3_1, any1)
    h2 = hybrid_payload(p2, raw2, raw2, e3_2, {})
    report["output_size"] = {
        "tokens_per_proxy_unit": round(ratio, 3),
        "calibration": {"poc1": [r1["usage"]["output_tokens"], round(proxy(p1) * ratio)],
                        "poc2": [r2["usage"]["output_tokens"], round(proxy(p2) * ratio)]},
        "hybrid_from_poc1_reply": round(proxy(h1) * ratio),
        "hybrid_from_poc2_reply": round(proxy(h2) * ratio),
        "parts_hybrid_poc1": {k: round(proxy(v) * ratio) for k, v in h1.items()},
        "parts_poc1_actual": {k: round(proxy(v) * ratio) for k, v in p1.items()},
        "parts_poc2_actual": {k: round(proxy(v) * ratio) for k, v in p2.items()},
    }
    (OUT / "hybrid_payload_example.json").write_text(json.dumps(h1, separators=(",", ":")), encoding="utf-8")
    (OUT / "replay.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
