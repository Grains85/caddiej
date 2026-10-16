"""Temporal POC 2: the vx14 draft with LOCATE as the single source of evidence.

Same shot, same 18+18 sheet, same model and reasoning effort as POC 1
(`poc_vx14.py`, whose loaders, grader and conclusion this imports unchanged).
Only the protocol differs:

* `locate` is nine positional entries, P1..P9, each null or
  `[DTL cell | null, FO cell | null, confidence 0..100]`.
* A checkpoint entry is `[score, confidence, direction | null]` -- no cell.
  CaddieJ derives the cell from `locate`: a `_dtl` list from that position's
  DTL cell, `_fo` from its FO cell, `_any` from DTL, else FO. A checkpoint
  whose position has no cell on that camera is refused `not_located`.
* JOB 1b carries a concise visual definition of each position.

Modes (exactly one paid call, ever):
  (default)   build and check the request, print its breakdown; no call
  --send      make THE call; refuses if output/poc2/reply.json exists
  --analyse   read output/poc2/reply.json through CaddieJ's parsers; no call

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\poc2_vx14b.py [--send|--analyse]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
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
from caddiej.domain.swing_phase import PHASE_ORDER
from caddiej.swing_capture.sheet import SHEET_LABEL

sys.path.insert(0, str(Path(__file__).parent))
import poc_vx14 as poc1  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "poc2"
REPLY = OUT / "reply.json"
POSITIONS = poc1.POSITIONS
CHECKPOINT_ENTRY = 3

_LOCATE_JOB = """\
JOB 1b - "locate": find the nine swing positions yourself, before rating anything.
The camera cells are stills of ONE swing in time order, left to right, top to
bottom, each camera on its own clock. A cell's ID and time only put it in
order; nothing printed on a cell says which position it shows. Look along each
camera's whole row before choosing.
"locate" has nine entries, P1 to P9 in this order. Each is null when neither
camera plainly shows that position, else [DTL cell or null, FO cell or null,
confidence 0..100 that those cells show it]. Never force a position: one that
fell between two stills, came before the first or after the last, or is
hidden is null on that camera.
P1 setup: address, club behind the ball, before the club moves.
P2 takeaway: shaft about parallel to the ground going back, hands about hip high.
P3 backswing: lead arm about parallel to the ground going back. NOT the top:
the hands are still rising, about chest high.
P4 top: the end of the backswing, where the hands stop rising and the club
changes direction.
P5 transition: lead arm about parallel to the ground again, coming down.
P6 downswing (delivery): shaft about parallel to the ground coming down,
before impact, hands about hip high.
P7 impact: clubhead at the ball. Choose the cell showing contact; between a
contact cell and the cell just after, prefer the last cell with the ball still
in its place. Never take the first cell after the ball only because contact
fell between two stills.
P8 release: shortly after the ball, shaft about parallel to the ground
pointing at the target, arms extending. Not any later cell: by the time the
club points up, it is past.
P9 finish: the balanced finish, turned to the target, club over the shoulder.
Along one camera the cells never go back in time from P1 to P9. One cell may
stand for two neighbouring positions only when the swing passed both within it.

"""

_SYSTEM_EDITS = (
    (
        "in that order: null, or [score, cell, confidence, direction]:",
        "in that order: null, or [score, confidence, direction]:",
    ),
    (
        '- cell: the printed ID of the ONE camera cell it was seen in, like "DTL_P5",\n'
        "  from that list's camera. Never IMPACT, never ALL_VALUES.\n",
        "",
    ),
    (
        "No sentences: CaddieJ knows each cell's camera, and takes its moment from your\n"
        '"frames" answer.',
        'No cell and no sentences: CaddieJ takes the picture from your "locate" answer.\n'
        "A _dtl list is read from that position's DTL cell, a _fo list from its FO\n"
        "cell, an _any list from its DTL cell, or its FO cell when DTL is null. Score\n"
        "only what that cell shows. When locate has no cell for that position on that\n"
        "camera, the entry is null.",
    ),
    (
        "This is checked against what YOU said\n"
        "each picture shows in JOB 1b: a checkpoint cited from a picture you called\n"
        "unclear, from the wrong moment or from the wrong view is thrown away.",
        "Each list is read\n"
        "only from the cell you located for its own position, so locate carefully:\n"
        "a wrong cell in locate makes every checkpoint of that position wrong.",
    ),
    (
        "Each swing picture's label ends with a moment and a time, like\n"
        '"~transition @2.9s". The moment is CaddieJ\'s guess from the clock, not a fact:\n'
        "it knows when the shutter went and has not looked at the picture.",
        'Each camera cell is labelled only with its ID and a time, like "DTL_07\n'
        '+1.43s": seconds from the start of that camera\'s replay. The time puts the\n'
        "cells in order; it does not say which moment a cell shows.",
    ),
)


def system_v14b() -> str:
    text = vx.SYSTEM
    start = text.index('JOB 1b - "frames"')
    end = text.index('JOB 2 - "checkpoints"')
    text = text[:start] + _LOCATE_JOB + text[end:]
    for old, new in _SYSTEM_EDITS:
        assert old in text, old[:60]
        text = text.replace(old, new)
    return text


def evidence_v14b(rows, cues) -> str:
    text = poc1.evidence_v14(rows, cues)
    old = "Only these may be given in locate or cited by a checkpoint:"
    assert old in text
    text = text.replace(old, "Only these may be given in locate:")
    old = "its id IS the printed cell ID: cite exactly that."
    assert old in text
    return text.replace(old, "its id IS the printed cell ID: name exactly that.")


def schema_v14b(base: dict, rows) -> dict:
    dtl = [r["cell_id"] for r in poc1.body(rows) if r["view"] == PanelView.BEHIND.value]
    fo = [r["cell_id"] for r in poc1.body(rows) if r["view"] == PanelView.SIDE.value]
    schema = json.loads(json.dumps(base))
    defs = {"dir": schema["$defs"]["dir"]}
    defs["entry"] = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "array",
                "minItems": CHECKPOINT_ENTRY,
                "maxItems": CHECKPOINT_ENTRY,
                "items": {
                    "anyOf": [
                        {"type": "integer", "minimum": 0, "maximum": 100},
                        {"$ref": "#/$defs/dir"},
                        {"type": "null"},
                    ]
                },
            },
        ]
    }
    position = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "anyOf": [
                        {"type": "string", "enum": dtl + fo},
                        {"type": "integer", "minimum": 0, "maximum": 100},
                        {"type": "null"},
                    ]
                },
            },
        ]
    }
    properties = schema["properties"]
    properties.pop("frames")
    checkpoints = properties["checkpoints"]
    for key, items in vx.WIRE_LISTS.items():
        checkpoints["properties"][key] = {
            "type": "array",
            "minItems": len(items),
            "maxItems": len(items),
            "items": {"$ref": "#/$defs/entry"},
        }
    schema["properties"] = {
        "locate": {"type": "array", "minItems": len(POSITIONS), "maxItems": len(POSITIONS), "items": position},
        **properties,
    }
    schema["required"] = ["locate", *[k for k in schema["required"] if k != "frames"]]
    schema["$defs"] = defs
    return schema


def build(rows, settings: Settings):
    cues = vx.possible_cues(ClubKind.UNKNOWN, has_metrics=True)
    cell_ids = tuple(r["cell_id"] for r in rows)
    body_ids = tuple(r["cell_id"] for r in poc1.body(rows))
    cell_views = {r["cell_id"]: PanelView(r["view"]) for r in poc1.body(rows)}
    image = ImageInput(data=poc1.SHEET.read_bytes(), label=SHEET_LABEL, detail="high")
    base = vx.build_request(
        evidence=evidence_v14b(rows, cues),
        images=(image,),
        cues=cues,
        max_output_tokens=settings.coaching_protee_max_output_tokens,
        timeout_seconds=settings.coach_openai_timeout,
        reasoning_effort=settings.coaching_reasoning_effort or None,
        frame_ids=cell_ids,
        body_frame_ids=body_ids,
        cell_views=cell_views,
    )
    return replace(base, system=system_v14b(), schema=schema_v14b(dict(base.schema), rows)), cues


# --- reading -------------------------------------------------------------------


def read_locate(raw, rows):
    """Positional locate -> per-camera lists, confidences, and validator notes."""

    notes: list[str] = []
    views = {r["cell_id"]: r["view"] for r in poc1.body(rows)}
    when = {r["cell_id"]: r["offset_from_replay_start_seconds"] for r in poc1.body(rows)}
    located = {"dtl": [None] * len(POSITIONS), "fo": [None] * len(POSITIONS)}
    confidence: list[int | None] = [None] * len(POSITIONS)
    if not isinstance(raw, list) or len(raw) != len(POSITIONS):
        return located, confidence, ["locate_length"]
    for i, entry in enumerate(raw):
        if entry is None:
            continue
        if not isinstance(entry, list) or len(entry) != 3:
            notes.append(f"{POSITIONS[i]}: locate_shape")
            continue
        d, f, c = entry
        if isinstance(c, bool) or not isinstance(c, int):
            notes.append(f"{POSITIONS[i]}: locate_confidence {c!r}")
            continue
        confidence[i] = c
        for key, cell, view in (("dtl", d, PanelView.BEHIND.value), ("fo", f, PanelView.SIDE.value)):
            if cell is None:
                continue
            if not isinstance(cell, str) or views.get(cell) != view:
                notes.append(f"{POSITIONS[i]} {key}: locate_cell {cell!r}")
                continue
            located[key][i] = cell
    for key in located:
        times = [when[c] for c in located[key] if c is not None]
        if any(b < a for a, b in zip(times, times[1:])):
            notes.append(f"{key}: locate_order -- whole camera dropped")
            located[key] = [None] * len(POSITIONS)
            continue
        for cell in {c for c in located[key] if c is not None}:
            at = [i for i, c in enumerate(located[key]) if c == cell]
            if len(at) > 2 or (len(at) == 2 and at[1] - at[0] != 1):
                notes.append(f"{key}: locate_shared {cell} at {[POSITIONS[i] for i in at]}")
                for i in at:
                    located[key][i] = None
    return located, confidence, notes


def derive_checkpoints(raw, located):
    """vx14b entries -> the named form parse_checkpoints reads, cells from locate."""

    index = {phase: i for i, phase in enumerate(PHASE_ORDER)}
    named: dict[str, object] = {}
    returned = 0
    if not isinstance(raw, dict):
        return named, returned
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        i = index[vx.SwingPhase(phase_name)]
        if camera == "dtl":
            cell = located["dtl"][i]
        elif camera == "fo":
            cell = located["fo"][i]
        else:
            cell = located["dtl"][i] or located["fo"][i]
        entries = raw.get(key)
        if not isinstance(entries, list) or len(entries) != len(specs):
            for spec in specs:
                named[spec.checkpoint_id.value] = vx.REFUSED + "length"
            continue
        for spec, entry in zip(specs, entries, strict=True):
            if entry is None:
                named[spec.checkpoint_id.value] = None
                continue
            returned += 1
            if not isinstance(entry, list) or len(entry) != CHECKPOINT_ENTRY:
                named[spec.checkpoint_id.value] = vx.REFUSED + "shape"
                continue
            score, conf, direction = entry
            if (
                isinstance(score, bool) or not isinstance(score, int)
                or isinstance(conf, bool) or not isinstance(conf, int)
                or not (direction is None or isinstance(direction, str))
            ):
                named[spec.checkpoint_id.value] = vx.REFUSED + "shape"
                continue
            if cell is None:
                named[spec.checkpoint_id.value] = vx.REFUSED + "not_located"
                continue
            named[spec.checkpoint_id.value] = {
                "score": score, "conf": conf / vx.CONFIDENCE_SCALE, "cell": cell, "dir": direction,
            }
    return named, returned


def read_checkpoints(payload, rows, located):
    cell_map = {r["cell_id"]: r["frame_id"] for r in rows}
    body_cells = [r["cell_id"] for r in poc1.body(rows)]
    frame_views = {r["frame_id"]: PanelView(r["view"]) for r in poc1.body(rows)}
    body_frames = tuple(r["frame_id"] for r in poc1.body(rows))
    first, second = poc1.invert(located)
    named, returned = derive_checkpoints(payload.get("checkpoints"), located)
    metrics_only = vx.decode_wire({k: v for k, v in payload.items() if k in ("metrics", "coach")}, body_cells)

    def parse(frames):
        canon = vx.canonical_reply({**metrics_only, "checkpoints": named, "frames": frames}, cell_map)
        return canon, vx.parse_checkpoints(
            canon["checkpoints"], body_frames=body_frames,
            frame_phases=vx.parse_frames(canon["frames"]), frame_views=frame_views,
        )

    canon, (assessments, rejected) = parse(first)
    if second:
        _, (again, _r) = parse({**first, **second})
        refused = {k for k, reason in rejected if reason == "phase"}
        rescued = [a for a in again if a.checkpoint.value in refused]
        if rescued:
            keep = {a.checkpoint.value for a in rescued}
            order = {item.checkpoint_id: n for n, item in enumerate(vx.SCOREABLE)}
            assessments = tuple(sorted((*assessments, *rescued), key=lambda a: order[a.checkpoint]))
            rejected = tuple(r for r in rejected if not (r[1] == "phase" and r[0] in keep))
    return canon, assessments, rejected, returned


async def send(request, settings: Settings) -> None:
    if REPLY.exists():
        raise SystemExit(f"{REPLY} exists: the one call has been made. Use --analyse.")
    OUT.mkdir(parents=True, exist_ok=True)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--analyse", action="store_true")
    options = parser.parse_args()
    settings = Settings()
    rows = poc1.load_cells()
    request, cues = build(rows, settings)
    problems = poc1.check_neutral(request)
    if problems:
        raise SystemExit(f"request is not neutral: {problems}")
    if options.send:
        asyncio.run(send(request, settings))
        return
    if not options.analyse:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "request_system.txt").write_text(request.system, encoding="utf-8")
        (OUT / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
        (OUT / "request_schema.json").write_text(json.dumps(request.schema, indent=1), encoding="utf-8")
        b = request_breakdown(request, model=settings.coaching_model)
        print(f"model {settings.coaching_model} effort {settings.coaching_reasoning_effort or None} "
              f"max_out {request.max_output_tokens} timeout {request.timeout_seconds}s; sheet {poc1.SHEET.name}")
        for t in b.texts:
            print(f"  {t.part:<20} {t.characters:6d} chars ~{t.approx_tokens} tok")
        print(f"  image ~{b.image_tokens}; text ~{b.text_tokens}; total ~{b.total_tokens}")
        return
    started = time.perf_counter()
    record = json.loads(REPLY.read_text(encoding="utf-8"))
    payload = record["payload"]
    located, confidence, notes = read_locate(payload.get("locate"), rows)
    canon, assessments, rejected, returned = read_checkpoints(payload, rows, located)
    concluded = poc1.conclude(canon, assessments, settings)
    seconds = time.perf_counter() - started
    table = poc1.grade(located, rows)
    for row in table:
        row["locate_confidence"] = confidence[POSITIONS.index(row["position"])]
    from collections import Counter

    from caddiej.coaching.score import score_swing
    from caddiej.domain.shot import Handedness

    metrics = vx.parse_metrics(canon.get("metrics"))
    score = score_swing(metrics, club=None, handedness=Handedness.UNKNOWN, checkpoints=assessments)
    mech = score.mechanics
    usage = record.get("usage") or {}
    result = {
        "raw_locate": payload.get("locate"),
        "locate_notes": notes,
        "localisation": table,
        "returned_checkpoints": returned,
        "accepted": len(assessments),
        "rejected": len(rejected),
        "rejection_reasons": dict(Counter(reason for _k, reason in rejected)),
        "rejected_list": [list(r) for r in rejected],
        "accepted_list": [
            {"checkpoint": a.checkpoint.value, "score": a.score, "confidence": a.confidence,
             "frame": a.frame_id, "phase": a.phase.value, "view": a.view.value,
             "deviation": None if a.deviation is None else a.deviation.value}
            for a in assessments
        ],
        "scoreable": len(vx.SCOREABLE),
        "mechanics": None if mech is None else {
            "value": mech.value, "raw_value": mech.raw_value, "coverage": round(mech.coverage, 3),
            "coverage_level": mech.coverage_level.value, "confidence": round(mech.confidence, 3),
            "ceiling": mech.ceiling, "ceiling_reasons": list(mech.ceiling_reasons),
            "phases": [
                {"phase": p.phase.value, "value": p.value, "coverage": round(p.coverage, 2),
                 "assessed": p.assessed, "scoreable": p.scoreable, "level": p.level.value,
                 "worst": None if p.worst is None else p.worst.value}
                for p in mech.phases
            ],
        },
        "swing_score": score.value,
        "concluded": {k: v for k, v in concluded.items() if k not in ("metrics", "mechanics")},
        "usage": usage,
        "cost_usd": estimate_cost(record["model"], usage.get("input_tokens"), usage.get("output_tokens")),
        "wall_seconds": record["wall_seconds"],
        "processing_seconds": round(seconds, 3),
    }
    (OUT / "analysis.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
