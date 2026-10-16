"""Temporal POC 4: vx14c group-owned evidence (VX14C_REPORT.md), one paid call.

Same shot, same 18+18 T36 sheet, same model and effort as POC 1-3. Only the
protocol differs: no LOCATE and no EVIDENCE layer; each phase writes
{d, f, a, s, x} -- its DTL and FO evidence cells, the camera of its _any
checkpoints, [score, confidence] per checkpoint, and a direction per checkpoint
from an enum with an explicit "none".

Modes (exactly one paid call, ever):
  (default)   build, check neutrality, write criteria + request texts; no call
  --send      make THE call; refuses if output/poc4/reply.json exists
  --analyse   read output/poc4/reply.json through the vx14c validator; no call
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching import vx
from caddiej.coaching.benchmark import MeteredOpenAIProvider, request_breakdown
from caddiej.coaching.provider import ImageInput
from caddiej.config import Settings
from caddiej.domain.cues import PanelView
from caddiej.domain.shot import ClubKind
from caddiej.swing_capture.sheet import SHEET_LABEL

sys.path.insert(0, str(Path(__file__).parent))
import hybrid_replay as hy  # noqa: E402
import poc2_vx14b as poc2  # noqa: E402
import poc3_hybrid as poc3  # noqa: E402
import poc_vx14 as poc1  # noqa: E402
import vx14c_replay as c  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "poc4"
REPLY = OUT / "reply.json"
CRITERIA = OUT / "criteria.json"
PHASES = c.PHASES
DIRECTIONS = ("none", *vx.WIRE_DIRECTIONS)
CAPTURE_SECONDS, RENDER_SECONDS = 4.53, 0.55

ADDRESS_REFERENCED = frozenset({
    "takeaway_posture", "backswing_posture", "backswing_hip_depth", "top_posture",
    "delivery_hip_depth", "impact_hip_depth", "impact_posture",
    "transition_posture_space", "release_posture", "backswing_centre", "top_centre",
    "transition_pressure_shift",
})

_JOB = """\
JOB 1b - "phases": for each swing position, choose its picture on each camera, then score it.
The camera cells are stills of ONE swing in time order, left to right, top to
bottom, each camera on its own clock. A cell's ID and time only put it in
order; nothing printed on a cell says which position it shows. Look along each
camera's whole row first. Write a cell as its number: DTL_13 is 13 in "d",
FO_13 is 13 in "f".
"phases" has one group per position. In each, "d" is the DTL cell that best
shows that position and "f" the FO cell, or 0 when no cell plainly shows it on
that camera (it fell between stills, came before the first or after the last,
or is hidden). Never borrow a cell from another position to fill a gap: 0 is
better than a cell of the wrong position. "a" names the camera, "d" or "f",
the phase's ANY checkpoints are judged on. Then score that phase's checkpoints
from those cells only (JOB 2).
P1 setup: address, club behind the ball, before the club moves.
P2 takeaway: going back, shaft about parallel to the ground, hands about hip high.
P3 backswing: before the top. Lead arm about horizontal going back; hands still
rising, about chest high; shaft pointing up, not yet behind the head.
P4 top: the hands at their highest and furthest back. The cell before shows
them still rising; the cell after shows them level or lower. During a pause,
any cell of the pause. Never a cell where the hands are still clearly rising.
P5 transition: after the top. Lead arm about horizontal again coming down;
hands clearly lower than at the top and moving toward the ball; shaft still
above the hands. P3 and P5 can look alike in one picture: the order decides.
P3 comes before the top, P5 after it.
P6 downswing (delivery): after P5, before impact. Shaft about horizontal, hands
near the trail hip, clubhead trailing the hands.
P7 impact: the clubhead at the ball, lead arm and shaft one straight line from
the shoulder to the clubhead, hands at or just ahead of the ball. Before it the
clubhead trails the hands; after it the clubhead is past the hands. Judge by
club and hands, not by whether a ball is visible: other balls may lie on the
mat. If contact fell between two cells, give the one whose clubhead is nearer
the ball, and if tied the earlier. Search hint only: on these replays impact
is usually among the cells from about +1.7s to +2.2s; the picture decides.
P8 release: after impact. Shaft about horizontal again, pointing at the
target, both arms straight, clubhead well past the hands. Not the contact
cell; once the shaft is vertical it is past.
P9 finish: the balanced finish, turned to the target, club over the shoulder.
Along one camera positions never go back in time from P1 to P9. One cell may
stand for two neighbouring positions only when the swing passed both within it.

"""

_EDITS = (
    ("numbered list per phase and camera.", "numbered list per phase."),
    (
        "A list named like top_dtl is read only from DTL_ cells, top_fo only from FO_\n"
        "cells, top_any from either. Each list has one entry per numbered checkpoint,\n"
        "in that order: null, or [score, cell, confidence, direction]:",
        'In each phase, "s" has one entry per numbered checkpoint, in that order:\n'
        'null, or [score, confidence]; "x" has one direction per numbered checkpoint,\n'
        "in the same order:",
    ),
    (
        '- cell: the printed ID of the ONE camera cell it was seen in, like "DTL_P5",\n'
        "  from that list's camera. Never IMPACT, never ALL_VALUES.\n",
        "",
    ),
    (
        "- direction: the word for which way it misses, from that checkpoint's dir\n"
        "  list, or null when it has none or scores 60 or more.",
        '- direction, in "x": the word for which way it misses, from that\n'
        "  checkpoint's dir list, or \"none\" when it has none, scores 60 or more,\n"
        "  or its entry in \"s\" is null.",
    ),
    (
        "No sentences: CaddieJ knows each cell's camera, and takes its moment from your\n"
        '"frames" answer.',
        'No cell and no sentences: CaddieJ takes the picture from the phase\'s own "d"\n'
        'or "f": DTL lines from "d", FO lines from "f", ANY lines from the camera in\n'
        '"a". Score only what that cell shows. When that cell is 0, those entries are\n'
        "null.",
    ),
    (
        "This is checked against what YOU said\n"
        "each picture shows in JOB 1b: a checkpoint cited from a picture you called\n"
        "unclear, from the wrong moment or from the wrong view is thrown away.",
        "CaddieJ checks each\n"
        "phase's cells against swing order and refuses a phase whose cell is out of\n"
        "place, with all its checkpoints.",
    ),
    poc2._SYSTEM_EDITS[4],
)


def system_v14c() -> str:
    text = vx.SYSTEM
    start = text.index('JOB 1b - "frames"')
    end = text.index('JOB 2 - "checkpoints"')
    text = text[:start] + _JOB + text[end:]
    for old, new in _EDITS:
        assert old in text, old[:60]
        text = text.replace(old, new)
    return text


def render_checkpoints() -> str:
    lines = [
        'Swing checkpoints, one numbered group per phase; "s" and "x" follow this '
        'numbering. DTL lines are read from that phase\'s "d" cell, FO lines from its '
        '"f" cell, ANY lines from the camera named in "a". Each line is: N key [camera] '
        "- what to look for and the functional range; its direction words."
    ]
    for p, phase in enumerate(PHASES):
        specs = c.specs_of(p)
        lines.append(f"{phase.value} ({len(specs)}):")
        for n, (cam, s) in enumerate(specs, start=1):
            club = " Null unless the whole club is visible." if s.club else ""
            dirs = "|".join(d.value for d in s.deviations) or "none"
            lines.append(f"{n} {s.checkpoint_id.value} [{cam.upper()}] - {s.observable}.{club} dir {dirs}")
    return "\n".join(lines)


def evidence_v14c(rows, cues) -> str:
    text = poc2.evidence_v14b(rows, cues)
    for old, new in (
        ("Only these may be given in locate:", 'Only these may be given in "d" and "f", by number:'),
        ("never in locate and never cited by a checkpoint", 'never in "d" or "f"'),
    ):
        assert old in text, old
        text = text.replace(old, new)
    start = text.index("Swing checkpoints, one numbered list per phase and camera")
    end = text.index("Cues you may choose.")
    return text[:start] + render_checkpoints() + "\n" + text[end:]


def phases_schema() -> dict:
    props = {}
    for p, phase in enumerate(PHASES):
        n = len(c.specs_of(p))
        props[phase.value] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["d", "f", "a", "s", "x"],
            "properties": {
                "d": {"type": "integer", "minimum": 0, "maximum": 18},
                "f": {"type": "integer", "minimum": 0, "maximum": 18},
                "a": {"type": "string", "enum": ["d", "f"]},
                "s": {"type": "array", "minItems": n, "maxItems": n, "items": {"anyOf": [
                    {"type": "null"},
                    {"type": "array", "minItems": 2, "maxItems": 2,
                     "items": {"type": "integer", "minimum": 0, "maximum": 100}},
                ]}},
                "x": {"type": "array", "minItems": n, "maxItems": n,
                      "items": {"type": "string", "enum": list(DIRECTIONS)}},
            },
        }
    return {"type": "object", "additionalProperties": False, "required": [ph.value for ph in PHASES],
            "properties": props}


def schema_v14c(base: dict) -> dict:
    schema = json.loads(json.dumps(base))
    properties = schema["properties"]
    properties.pop("frames")
    properties.pop("checkpoints")
    schema["properties"] = {"phases": phases_schema(), **properties}
    schema["required"] = ["phases", *[k for k in schema["required"] if k not in ("frames", "checkpoints")]]
    schema.pop("$defs", None)
    return schema


def build(rows, settings: Settings):
    cues = vx.possible_cues(ClubKind.UNKNOWN, has_metrics=True)
    image = ImageInput(data=poc1.SHEET.read_bytes(), label=SHEET_LABEL, detail="high")
    base = vx.build_request(
        evidence=evidence_v14c(rows, cues),
        images=(image,),
        cues=cues,
        max_output_tokens=settings.coaching_protee_max_output_tokens,
        timeout_seconds=settings.coach_openai_timeout,
        reasoning_effort=settings.coaching_reasoning_effort or None,
        frame_ids=tuple(r["cell_id"] for r in rows),
        body_frame_ids=tuple(r["cell_id"] for r in poc1.body(rows)),
        cell_views={r["cell_id"]: PanelView(r["view"]) for r in poc1.body(rows)},
    )
    return replace(base, system=system_v14c(), schema=schema_v14c(dict(base.schema))), cues


PASS_CRITERIA = {
    "source": "VX14C_REPORT.md section 12, unchanged",
    "wrong_group_cells_max": 1,
    "wrong_group_definition": "returned (non-zero) phase/camera group cells graded 'wrong' under POC 1's TRUTH/RULE "
                              "(neighbour still of a correct still = acceptable); 0 cells are unavailable, not wrong",
    "p7_both_cameras_capture_index_in": [40, 41, 42],
    "p7_definition": "impact group cell on DTL and on FO each within one captured still of contact (#41)",
    "accepted_min": 40,
    "wrong_position_accepted_max": 3,
    "wrong_position_definition": "accepted checkpoint whose group cell is graded 'wrong' for its phase",
    "wire_shape_rejections_max": 0,
    "wire_shape_definition": "checkpoints refused as length/shape/camera before any evidence or order rule",
    "output_tokens_max": 1000,
    "output_tokens_note": "approximately: 1001-1100 reported as borderline, >1100 fails",
    "validator": "vx14c_replay G1-G4 (hybrid_replay.R7 leave-one-out, TAU), no gap filling; R1 address reference",
}


# --- reading ---------------------------------------------------------------------


def read_reply(payload, cams):
    groups = {"dtl": [None] * 9, "fo": [None] * 9}
    any_cam, entries, returned = {}, [], 0
    phases = payload.get("phases") or {}
    for p, phase in enumerate(PHASES):
        specs = c.specs_of(p)
        g = phases.get(phase.value)
        if not isinstance(g, dict):
            entries += [(s, None, p, vx.REFUSED + "shape") for _c, s in specs]
            continue
        for key, field in (("dtl", "d"), ("fo", "f")):
            cell = poc3.num_to_cell(cams, key, g.get(field))
            groups[key][p] = None if cell == "invalid" else cell
        a = {"d": "dtl", "f": "fo"}.get(g.get("a"))
        any_cam[p] = a
        s_list, x_list = g.get("s"), g.get("x")
        if not (isinstance(s_list, list) and isinstance(x_list, list)
                and len(s_list) == len(specs) == len(x_list)) or a is None:
            entries += [(s, None, p, vx.REFUSED + "length") for _c, s in specs]
            continue
        for (cam, spec), sc, x in zip(specs, s_list, x_list, strict=True):
            owner = a if cam == "any" else cam
            if sc is None:
                entries.append((spec, owner, p, None))
                continue
            returned += 1
            if not (isinstance(sc, list) and len(sc) == 2 and all(isinstance(v, int) for v in sc)
                    and x in DIRECTIONS):
                entries.append((spec, owner, p, vx.REFUSED + "shape"))
                continue
            if groups[owner][p] is None:
                entries.append((spec, owner, p, vx.REFUSED + "no_evidence"))
                continue
            entries.append((spec, owner, p, {"score": sc[0], "conf": sc[1] / 100,
                                            "dir": None if x == "none" else x}))
    return groups, any_cam, entries, returned


def apply_reference(entries, kept):
    out = []
    for spec, cam, p, item in entries:
        if isinstance(item, dict) and spec.checkpoint_id.value in ADDRESS_REFERENCED and kept[cam][0] is None:
            out.append((spec, cam, p, vx.REFUSED + "no_reference"))
        else:
            out.append((spec, cam, p, item))
    return out


async def send(request, settings: Settings) -> None:
    if REPLY.exists():
        raise SystemExit(f"{REPLY} exists: the one call has been made. Use --analyse.")
    if not CRITERIA.exists():
        raise SystemExit("criteria not written: run the dry build first")
    REPLY.write_text(json.dumps({"status": "in_flight", "started_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    provider = MeteredOpenAIProvider(
        api_key=settings.coaching_api_key.get_secret_value(),
        model=settings.coaching_model,
        base_url=settings.coaching_api_base or None,
    )
    started = time.perf_counter()
    reply, error = None, None
    try:
        reply = await provider.complete(request)
    except Exception as exc:  # recorded, never retried
        error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - started
    usage = provider.usage()
    await provider.aclose()
    record = {
        "status": "ok" if reply is not None else "error", "error": error,
        "model": settings.coaching_model, "reasoning_effort": settings.coaching_reasoning_effort or None,
        "max_output_tokens": request.max_output_tokens, "timeout_seconds": request.timeout_seconds,
        "wall_seconds": round(wall, 3),
        "usage": None if usage is None else asdict(usage),
        "payload": None if reply is None else reply.payload,
    }
    REPLY.write_text(json.dumps(record, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in record.items() if k != "payload"}, indent=1))


def analyse(settings: Settings, rows) -> dict:
    record = json.loads(REPLY.read_text(encoding="utf-8"))
    payload = record["payload"]
    cams = hy.sheet(rows)
    started = time.perf_counter()
    groups, any_cam, entries, returned = read_reply(payload, cams)
    kept, notes = c.validate_groups(groups, cams)
    entries = apply_reference(entries, kept)
    accepted, rejected = c.assess(entries, kept, cams)
    canon = vx.canonical_reply({**vx.decode_wire({k: v for k, v in payload.items() if k in ("metrics", "coach")}, []),
                                "checkpoints": {}, "frames": {}}, {r["cell_id"]: r["frame_id"] for r in rows})
    validate_seconds = time.perf_counter() - started
    summary = hy.summary("POC4 vx14c", accepted, rejected, canon, rows, settings, {"rejected_list": rejected})
    group_rows = []
    for cam in ("dtl", "fo"):
        for p in range(9):
            cell = groups[cam][p]
            ix, verdict = poc3.grade_cell(rows, cam, p, cell)
            n_spec = sum(1 for s, owner, q, _i in entries if owner == cam and q == p)
            group_rows.append({"phase": PHASES[p].value, "position": poc1.POSITIONS[p], "camera": cam,
                               "cell": cell, "capture_index": ix, "rule_verdict": verdict,
                               "kept": kept[cam][p] is not None, "checkpoints_on_group": n_spec})
    returned_groups = [g for g in group_rows if g["cell"] is not None]
    wrong_groups = sum(g["rule_verdict"] == "wrong" for g in returned_groups)
    p7 = {g["camera"]: g["capture_index"] for g in group_rows if g["position"] == "P7"}
    shape = sum(1 for _k, r in rejected if r in ("length", "shape", "camera"))
    usage = record.get("usage") or {}
    out_tokens = usage.get("output_tokens") or 0
    crit = json.loads(CRITERIA.read_text(encoding="utf-8"))["criteria"]
    verdicts = {
        "wrong_group_cells": [wrong_groups, wrong_groups <= crit["wrong_group_cells_max"]],
        "p7": [p7, all(v in crit["p7_both_cameras_capture_index_in"] for v in p7.values())],
        "accepted": [summary["accepted"], summary["accepted"] >= crit["accepted_min"]],
        "wrong_position_accepted": [len(summary["accepted_wrong_position"]),
                                    len(summary["accepted_wrong_position"]) <= crit["wrong_position_accepted_max"]],
        "wire_shape_rejections": [shape, shape <= crit["wire_shape_rejections_max"]],
        "output_tokens": [out_tokens, "pass" if out_tokens <= 1000 else "borderline" if out_tokens <= 1100 else "fail"],
    }
    result = {
        "raw_phases": {k: {kk: v[kk] for kk in ("d", "f", "a")} for k, v in (payload.get("phases") or {}).items()},
        "group_notes": notes,
        "groups": group_rows,
        "group_counts": dict(Counter(g["rule_verdict"] for g in group_rows)),
        "returned_checkpoints": returned,
        "summary": summary,
        "usage": usage,
        "cost_usd": estimate_cost(record["model"], usage.get("input_tokens"), usage.get("output_tokens")),
        "wall_seconds": record["wall_seconds"],
        "validate_seconds": round(validate_seconds, 3),
        "strike_to_verdict_seconds": round(CAPTURE_SECONDS + RENDER_SECONDS + record["wall_seconds"] + validate_seconds, 2),
        "criteria_verdicts": verdicts,
    }
    (OUT / "analysis.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--analyse", action="store_true")
    options = parser.parse_args()
    settings = Settings()
    rows = poc1.load_cells()
    request, _cues = build(rows, settings)
    problems = poc1.check_neutral(request)
    if problems:
        raise SystemExit(f"request is not neutral: {problems}")
    if options.send:
        asyncio.run(send(request, settings))
        return
    if options.analyse:
        print(json.dumps(analyse(settings, rows), indent=1, default=str))
        return
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "request_system.txt").write_text(request.system, encoding="utf-8")
    (OUT / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
    (OUT / "request_schema.json").write_text(json.dumps(request.schema, indent=1), encoding="utf-8")
    if not REPLY.exists():
        CRITERIA.write_text(json.dumps({"written_at": datetime.now(UTC).isoformat(), "sheet": poc1.SHEET.name,
                                        "criteria": PASS_CRITERIA}, indent=1), encoding="utf-8")
    b = request_breakdown(request, model=settings.coaching_model)
    print(f"model {settings.coaching_model} effort {settings.coaching_reasoning_effort or None} "
          f"max_out {request.max_output_tokens} timeout {request.timeout_seconds}s; sheet {poc1.SHEET.name} "
          f"{poc1.SHEET.stat().st_size} B")
    for t in b.texts:
        print(f"  {t.part:<20} {t.characters:6d} chars ~{t.approx_tokens} tok")
    print(f"  image ~{b.image_tokens}; text ~{b.text_tokens}; total ~{b.total_tokens}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
