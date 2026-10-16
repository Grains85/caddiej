"""Temporal Evidence V2, offline: the five frozen fresh shots, no call.

1. The P3-P6 diagnosis: per shot and position, the best capture tick read by
   eye on the canonical full-resolution originals (all 51 ticks, both cameras,
   not only the 18 on the sheet), against the model's frozen choice.
2. The frozen replies decoded exactly as fresh_run.analyse does, through the
   frozen V1 validator and through V2 (v2_order), with M4 recomputed from the
   frozen grades. M1-M3 grade the raw model choice and cannot move offline.

Reads fresh/ only; writes output/v2/offline.json.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\v2_offline.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import fresh_run as fr  # noqa: E402
import hybrid_replay as hy  # noqa: E402
import poc4_vx14c as poc4  # noqa: E402
import v2_order  # noqa: E402
import vx14c_replay as c  # noqa: E402

OUT = Path(__file__).with_name("output") / "v2"
SHOTS = (
    "20261008T141040-16-74c698a2",
    "20261008T141833-19-504f8f24",
    "20261008T142000-20-c70f8efc",
    "20261008T142053-21-7f8f4191",
    "20261008T142305-23-3d49f492",
)

# By-eye reading of the canonical originals (face-on for the arm, both views for
# the shaft). best: the best capture tick; ok: every tick within the position's
# own window; P4 is the top pause. DTL and FO share ticks, so one reading per shot.
READINGS: dict[str, dict[str, Any]] = {
    "20261008T141040-16-74c698a2": {
        "P3": {"best": 24, "ok": [24]},
        "P4": {"best": 33, "ok": list(range(29, 37))},
        "P5": {"best": 39, "ok": [39]},
        "P6": {
            "best": 40,
            "ok": [40],
            "note": "shaft ~55 deg up at 40, contact at 41: horizontal fell between ticks",
        },
        "contact": 41,
    },
    "20261008T141833-19-504f8f24": {
        "P3": {"best": 25, "ok": [24, 25, 26]},
        "P4": {"best": 33, "ok": list(range(30, 38))},
        "P5": {"best": 39, "ok": [39]},
        "P6": {"best": 40, "ok": [40], "note": "between 40 and contact 41"},
        "contact": 41,
    },
    "20261008T142000-20-c70f8efc": {
        "P3": {"best": 25, "ok": [24, 25, 26]},
        "P4": {"best": 33, "ok": list(range(30, 37))},
        "P5": {"best": 39, "ok": [38, 39]},
        "P6": {
            "best": 40,
            "ok": [40],
            "note": "between 39 and 40; shaft already below horizontal at 40",
        },
        "contact": 41,
    },
    "20261008T142053-21-7f8f4191": {
        "P3": {"best": 25, "ok": [24, 25, 26]},
        "P4": {"best": 32, "ok": list(range(29, 37))},
        "P5": {"best": 38, "ok": [38]},
        "P6": {"best": 39, "ok": [39], "note": "between 39 and contact 40"},
        "contact": 40,
    },
    "20261008T142305-23-3d49f492": {
        "P3": {"best": 25, "ok": [25, 26]},
        "P4": {"best": 33, "ok": list(range(29, 38))},
        "P5": {"best": 39, "ok": [39]},
        "P6": {"best": 41, "ok": [40, 41], "note": "between 40 and 41"},
        "contact": 42,
        "contact_note": "DTL: clubhead at the ball on 42, ball gone from the mat on 43, "
        "screen hit on 44. Frozen grades.json says 44.",
    },
}


def _load(name: str, root: Path = fr.FRESH) -> dict[str, Any]:
    out = root / "runs" / name
    return {
        "reply": fr._json(out / "reply.json"),
        "rows": fr._usable(fr._json(out / "cells.json"))[0],
        "meta": fr._json(out / "request_meta.json"),
        "grades": fr._json(out / "grades.json"),
        "analysis": fr._json(out / "analysis.json"),
    }


def replay(name: str, validate: Any, root: Path = fr.FRESH) -> dict[str, Any]:
    """fresh_run.analyse's decode -> validate -> reference -> assess, with `validate` swapped."""

    config, _criteria = fr.load_frozen(root)
    hy.R7 = tuple(config["impact_prior"]["range_s"])
    run = _load(name, root)
    cams = hy.sheet(run["rows"])
    payload = run["reply"].get("payload") or {}
    groups, _any, entries, _returned, _notes = fr.decode(payload, cams)
    kept, notes = validate(groups, cams)
    entries = poc4.apply_reference(entries, kept)
    accepted, rejected = c.assess(entries, kept, cams)
    frame_cam = {
        r["frame_id"]: r["camera_id"] for r in run["rows"] if "capture_index" in r
    }
    owners = [
        (
            fr.CAMERA_KEY[frame_cam[a.frame_id]],
            fr.POSITIONS[[ph.value for ph in fr.PHASES].index(a.phase.value)],
        )
        for a in accepted
    ]
    return {
        "groups": groups,
        "kept": kept,
        "notes": notes,
        "owners": owners,
        "rejected": rejected,
    }


def m4(names: tuple[str, ...], validate: Any, root: Path = fr.FRESH) -> dict[str, Any]:
    wrong = total = 0
    per_shot = {}
    for name in names:
        grades = _load(name, root)["grades"]["groups"]
        r = replay(name, validate, root)
        w = sum(grades[cam][pos]["grade"] == "wrong" for cam, pos in r["owners"])
        per_shot[name] = {"accepted": len(r["owners"]), "wrong": w, "notes": r["notes"]}
        wrong += w
        total += len(r["owners"])
    return {
        "wrong": wrong,
        "accepted": total,
        "ratio": wrong / total if total else None,
        "per_shot": per_shot,
    }


def diagnosis(
    names: tuple[str, ...] = SHOTS, root: Path = fr.FRESH
) -> list[dict[str, Any]]:
    table = []
    for name in names:
        run = _load(name, root)
        cell_of_tick = {m["tick"]: m["number"] for m in run["meta"]["sheet"]["moments"]}
        grades = run["grades"]["groups"]
        for pos in ("P3", "P4", "P5", "P6"):
            reading = READINGS[name][pos]
            picks = {cam: grades[cam][pos]["capture_index"] for cam in ("dtl", "fo")}
            pick = picks["fo"]
            on_sheet = [t for t in reading["ok"] if t in cell_of_tick]
            best_on_sheet = (
                min(on_sheet, key=lambda t: abs(t - reading["best"]))
                if on_sheet
                else None
            )
            table.append(
                {
                    "shot": name,
                    "position": pos,
                    "best_tick": reading["best"],
                    "best_cell": cell_of_tick.get(reading["best"]),
                    "best_on_sheet": [best_on_sheet, cell_of_tick.get(best_on_sheet)],
                    "model_tick": pick,
                    "model_cell": cell_of_tick.get(pick),
                    "same_on_both_cameras": picks["dtl"] == picks["fo"],
                    "delta_ticks": None if pick is None else pick - reading["best"],
                    "delta_cells": None
                    if pick is None or best_on_sheet is None
                    else cell_of_tick[pick] - cell_of_tick[best_on_sheet],
                    "candidate_on_sheet": bool(on_sheet),
                    "frozen_grade": [
                        grades["dtl"][pos]["grade"],
                        grades["fo"][pos]["grade"],
                    ],
                    "note": reading.get("note"),
                }
            )
    return table


def main() -> None:
    table = diagnosis()
    v1 = m4(SHOTS, c.validate_groups)
    v2 = m4(SHOTS, v2_order.validate_groups)
    frozen = fr._json(fr.FRESH / "evaluation.json")["metrics"][
        "M4_wrong_position_accepts"
    ]
    report = {
        "diagnosis": table,
        "contact": {
            n: [READINGS[n]["contact"], _load(n)["grades"]["contact_tick"]]
            for n in SHOTS
        },
        "M4_frozen": frozen,
        "M4_v1_replayed": [v1["ratio"], f"{v1['wrong']}/{v1['accepted']}"],
        "M4_v2_validator": [v2["ratio"], f"{v2['wrong']}/{v2['accepted']}"],
        "v2_notes": {n: s["notes"] for n, s in v2["per_shot"].items() if s["notes"]},
        "per_shot": {
            n: [
                v1["per_shot"][n]["wrong"],
                v1["per_shot"][n]["accepted"],
                v2["per_shot"][n]["wrong"],
                v2["per_shot"][n]["accepted"],
            ]
            for n in SHOTS
        },
        "candidates_on_sheet": dict(Counter(r["candidate_on_sheet"] for r in table)),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "offline.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(
        f"{'shot':<8}{'pos':<5}{'best':>6}{'cell':>6}{'model':>7}{'cell':>6}{'dT':>5}{'dC':>5}  on-sheet  frozen grade"
    )
    for r in table:
        print(
            f"{r['shot'].split('-')[1]:<8}{r['position']:<5}{r['best_tick']:>6}{str(r['best_on_sheet'][1]):>6}"
            f"{r['model_tick']:>7}{str(r['model_cell']):>6}{r['delta_ticks']:>+5}{str(r['delta_cells']):>5}"
            f"  {str(r['candidate_on_sheet']):<8}  {'/'.join(r['frozen_grade'])}"
        )
    print("contact (by-eye V2 reading, frozen grade):", report["contact"])
    print(
        "M4 frozen",
        frozen[2],
        "| V1 replayed",
        report["M4_v1_replayed"],
        "| V2 validator",
        report["M4_v2_validator"],
    )
    print("V2 strict-order notes:", report["v2_notes"])
    print(
        "per shot [V1 wrong, V1 accepted, V2 wrong, V2 accepted]:", report["per_shot"]
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()
