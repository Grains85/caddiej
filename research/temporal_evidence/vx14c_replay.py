"""Offline replay of the saved POC replies through vx14c: group-owned evidence. No call.

vx14c: per phase, one DTL evidence cell and one FO evidence cell, written together
with that phase's scores; `_any` checkpoints ride with the camera named by a typed
per-phase field. No LOCATE (variant A). Variant B adds POC 1's coarse LOCATE as a
region check only.

Validation of the group cells, per camera, every failure local to one group:
  G1 cell is a real cell of that camera (schema: 0..18, 0 = no evidence);
  G2 broad replay-impact prior: impact inside R7, pre-impact groups before R7's end,
     post-impact groups after its start (hybrid_replay.R7, leave-one-out);
  G3 local order: keep groups present in every longest time-ordered chain, null the rest;
  G4 cross-camera agreement for moving positions (hybrid_replay.TAU, bracket tie-break).
A nulled or empty group makes ALL its checkpoints unavailable. No gap filling.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

from caddiej.coaching import vx
from caddiej.config import Settings
from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import PHASE_ORDER

sys.path.insert(0, str(Path(__file__).parent))
import hybrid_replay as hy  # noqa: E402
import poc2_vx14b as poc2  # noqa: E402
import poc3_hybrid as poc3  # noqa: E402
import poc3_salvage as salvage  # noqa: E402
import poc_vx14 as poc1  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "vx14c"
PHASES = list(PHASE_ORDER)


def specs_of(p):
    """[(list camera 'dtl'|'fo'|'any', spec)] for phase index p, catalogue order."""

    out = []
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        if PHASES.index(vx.SwingPhase(phase_name)) == p:
            out += [(camera, s) for s in specs]
    return out


def validate_groups(groups, cams):
    """Group cells -> kept cells and notes. Same local rules as the hybrid LOCATE validator."""

    return hy.validate_locate({k: list(v) for k, v in groups.items()}, cams)


def assess(entries, kept, cams, region_check=None):
    """entries: [(spec, cam, phase_index, named item | None | refused)]."""

    frame_views = {cams[k]["frame"][c]: PanelView.BEHIND if k == "dtl" else PanelView.SIDE
                   for k in cams for c in cams[k]["cells"]}
    accepted, rejected = [], []
    for spec, cam, p, item in entries:
        cid = spec.checkpoint_id.value
        if item is None:
            continue
        if isinstance(item, str):
            rejected.append((cid, item.removeprefix(vx.REFUSED)))
            continue
        cell = kept[cam][p] if cam else None
        if cell is None:
            rejected.append((cid, "group_unavailable"))
            continue
        if region_check is not None and not region_check(cam, p, cell):
            rejected.append((cid, "outside_locate_region"))
            continue
        frame = cams[cam]["frame"][cell]
        got, rej = vx.parse_checkpoints({cid: {**item, "cell": frame}}, body_frames=tuple(frame_views),
                                        frame_phases={frame: PHASES[p]}, frame_views=frame_views)
        (rejected.append(rej[0]) if rej else accepted.append(got[0]))
    return accepted, rejected


def grade_groups(groups, kept, rows):
    out = []
    for cam in ("dtl", "fo"):
        for p in range(9):
            cell = groups[cam][p]
            if cell is None:
                continue
            ix, verdict = poc3.grade_cell(rows, cam, p, cell)
            out.append({"camera": cam, "position": poc1.POSITIONS[p], "cell": cell, "capture_index": ix,
                        "truth": verdict, "kept": kept[cam][p] is not None})
    return out


# --- mapping the saved replies into groups (no cell is invented) ---------------


def from_poc1(payload):
    named = vx.decode_wire({"checkpoints": payload["checkpoints"]}, [])["checkpoints"]
    groups = {"dtl": [None] * 9, "fo": [None] * 9}
    any_cam = {}
    entries, splits, list_cells = [], [], {}
    for p in range(9):
        own = {"dtl": None, "fo": None}
        items = [(c, s, named.get(s.checkpoint_id.value)) for c, s in specs_of(p)]
        for camera in ("dtl", "fo"):
            cells = {it["cell"] for c, s, it in items if c == camera and isinstance(it, dict)}
            list_cells[f"{PHASES[p].value}_{camera}"] = sorted(cells)
            if cells:
                own[camera] = sorted(cells)[0]
                groups[camera][p] = own[camera]
        any_cells = {it["cell"] for c, s, it in items if c == "any" and isinstance(it, dict)}
        list_cells[f"{PHASES[p].value}_any"] = sorted(any_cells)
        a_cam, a_cell = None, None
        if any_cells:
            a_cell = sorted(any_cells)[0]
            a_cam = "dtl" if a_cell.startswith("DTL_") else "fo"
            any_cam[p] = a_cam
            if groups[a_cam][p] is None:
                groups[a_cam][p] = a_cell
            elif groups[a_cam][p] != a_cell:
                splits.append({"phase": PHASES[p].value, "camera": a_cam,
                               "camera_list_cell": groups[a_cam][p], "any_list_cell": a_cell})
        for c, s, it in items:
            cam = c if c != "any" else a_cam
            if isinstance(it, dict) and c == "any" and a_cell != groups[a_cam][p]:
                entries.append((s, cam, p, vx.REFUSED + "needs_second_cell"))
            elif isinstance(it, dict):
                entries.append((s, cam, p, {k: v for k, v in it.items() if k != "cell"}))
            else:
                entries.append((s, cam, p, it))
    return groups, any_cam, entries, splits, list_cells


def from_poc2(payload, rows):
    cams = hy.sheet(rows)
    groups = {"dtl": [None if e is None else e[0] for e in payload["locate"]],
              "fo": [None if e is None else e[1] for e in payload["locate"]]}
    kept_for_any, _ = hy.validate_locate({k: list(v) for k, v in groups.items()}, cams)
    entries = []
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        p = PHASES.index(vx.SwingPhase(phase_name))
        cam = camera if camera != "any" else ("dtl" if kept_for_any["dtl"][p] else "fo")
        for s, e in zip(specs, payload["checkpoints"].get(key) or [None] * len(specs), strict=True):
            entries.append((s, cam, p, None if e is None else {"score": e[0], "conf": e[1] / 100, "dir": e[2]}))
    return groups, entries


def from_poc3(payload, rows):
    cams = hy.sheet(rows)
    cleaned = salvage.clean(payload)
    _locate, evidence = poc3.grids(cleaned, cams)
    entries = []
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        p = PHASES.index(vx.SwingPhase(phase_name))
        lst = cleaned["checkpoints"].get(key) or []
        if camera == "any":
            if not lst or lst[0] not in (1, 2):
                entries += [(s, None, p, vx.REFUSED + "length") for s in specs]
                continue
            cam, lst = ("dtl" if lst[0] == 1 else "fo"), lst[1:]
        else:
            cam = camera
        if len(lst) != len(specs):
            entries += [(s, cam, p, vx.REFUSED + "length") for s in specs]
            continue
        for s, e in zip(specs, lst, strict=True):
            entries.append((s, cam, p, None if e is None else {"score": e[0], "conf": e[1] / 100, "dir": e[2]}))
    return evidence, entries


# --- output size -----------------------------------------------------------------


def vx14c_payload(payload, groups, any_cam, entries):
    """The proposed wire for one reply's content."""

    by_spec = {s.checkpoint_id.value: it for s, _c, _p, it in entries}
    out = {}
    for p in range(9):
        s_list, x_list = [], []
        for _c, s in specs_of(p):
            it = by_spec.get(s.checkpoint_id.value)
            if isinstance(it, dict):
                s_list.append([it["score"], round(it["conf"] * 100)])
                x_list.append(it.get("dir") or "none")
            else:
                s_list.append(None)
                x_list.append("none")

        def num(cell):
            return 0 if cell is None else int(cell.split("_")[1])

        out[PHASES[p].value] = {"d": num(groups["dtl"][p]), "f": num(groups["fo"][p]),
                                "a": "f" if any_cam.get(p) == "fo" else "d", "s": s_list, "x": x_list}
    return {"phases": out, "metrics": payload["metrics"], "coach": payload["coach"]}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    rows = poc1.load_cells()
    cams = hy.sheet(rows)
    report = {"R7": [round(x, 3) for x in hy.R7], "TAU": hy.TAU}

    # POC 1 -- the primary source: independently selected cells
    r1 = json.loads((HERE / "output/poc/reply.json").read_text(encoding="utf-8"))
    p1 = r1["payload"]
    old_loc, _ = poc1.validate_locate(p1["locate"], rows)
    canon1, _a, _r = poc1.read_checkpoints(p1, rows, old_loc)
    groups1, any1, entries1, splits1, list_cells1 = from_poc1(p1)
    kept1, notes1 = validate_groups(groups1, cams)
    acc_a, rej_a = assess(entries1, kept1, cams)
    loc_kept, _ = hy.validate_locate(p1["locate"], cams)

    def in_locate_region(cam, p, cell):
        if loc_kept[cam][p] is None:
            return True
        allowed, _anchored = hy.region(cams, loc_kept, cam, p)
        return cell in allowed

    acc_b, rej_b = assess(entries1, kept1, cams, region_check=in_locate_region)
    report["poc1"] = {
        "list_cells": list_cells1,
        "splits": splits1,
        "groups": grade_groups(groups1, kept1, rows),
        "group_notes": notes1,
        "A_no_locate": hy.summary("POC1 vx14c A", acc_a, rej_a, canon1, rows, settings, {"rejected_list": rej_a}),
        "B_coarse_locate": hy.summary("POC1 vx14c B", acc_b, rej_b, canon1, rows, settings, {"rejected_list": rej_b}),
    }

    # POC 2 -- its locate cells are the cells it said every checkpoint was read from
    r2 = json.loads((HERE / "output/poc2/reply.json").read_text(encoding="utf-8"))
    p2 = r2["payload"]
    loc2, _c, _n = poc2.read_locate(p2["locate"], rows)
    canon2, _a2, _r2, _ret = poc2.read_checkpoints(p2, rows, loc2)
    groups2, entries2 = from_poc2(p2, rows)
    kept2, notes2 = validate_groups(groups2, cams)
    acc2, rej2 = assess(entries2, kept2, cams)
    report["poc2"] = {"groups": grade_groups(groups2, kept2, rows), "group_notes": notes2,
                      "A_no_locate": hy.summary("POC2 vx14c A", acc2, rej2, canon2, rows, settings)}

    # POC 3 -- its evidence grid (= its locate) is per phase/camera; camera digit read where it was written
    r3 = json.loads((HERE / "output/poc3/reply.json").read_text(encoding="utf-8"))
    p3 = r3["payload"]
    groups3, entries3 = from_poc3(p3, rows)
    kept3, notes3 = validate_groups(groups3, cams)
    acc3, rej3 = assess(entries3, kept3, cams)
    canon3 = vx.canonical_reply({**vx.decode_wire({k: v for k, v in p3.items() if k in ("metrics", "coach")}, []),
                                 "checkpoints": {}, "frames": {}}, {r["cell_id"]: r["frame_id"] for r in rows})
    report["poc3"] = {"groups": grade_groups(groups3, kept3, rows), "group_notes": notes3,
                      "A_no_locate": hy.summary("POC3 vx14c A (salvaged wire)", acc3, rej3, canon3, rows, settings)}

    # size
    ratio = mean([r1["usage"]["output_tokens"] / hy.proxy(p1), r2["usage"]["output_tokens"] / hy.proxy(p2),
                  r3["usage"]["output_tokens"] / hy.proxy(p3)])
    w1 = vx14c_payload(p1, groups1, any1, entries1)
    any3 = {}
    cleaned3 = salvage.clean(p3)
    for key in vx.WIRE_LISTS:
        if key.endswith("_any"):
            lst = cleaned3["checkpoints"].get(key) or []
            if lst and lst[0] in (1, 2):
                any3[PHASES.index(vx.SwingPhase(key.rsplit("_", 1)[0]))] = "dtl" if lst[0] == 1 else "fo"
    w3 = vx14c_payload(p3, groups3, any3, entries3)
    report["output_size"] = {
        "tokens_per_proxy_unit": round(ratio, 3),
        "calibration_actual_vs_proxy": {
            "poc1": [r1["usage"]["output_tokens"], round(hy.proxy(p1) * ratio)],
            "poc2": [r2["usage"]["output_tokens"], round(hy.proxy(p2) * ratio)],
            "poc3": [r3["usage"]["output_tokens"], round(hy.proxy(p3) * ratio)],
        },
        "vx14c_from_poc1": round(hy.proxy(w1) * ratio),
        "vx14c_from_poc3": round(hy.proxy(w3) * ratio),
        "vx14c_parts_poc1": {k: round(hy.proxy(v) * ratio) for k, v in w1.items()},
        "variant_B_locate_extra": round(hy.proxy({"locate": [[1, 1]] * 9}) * ratio),
    }
    (OUT / "vx14c_payload_example.json").write_text(json.dumps(w1, separators=(",", ":")), encoding="utf-8")
    (OUT / "replay.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
