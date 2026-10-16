"""Temporal POC 3: the vx14 HYBRID protocol (HYBRID_REPORT.md), one paid call.

Same shot, same 18+18 T36 sheet, same model and effort as POC 1 and 2. Only the
protocol differs:

* "locate":   nine [DTL, FO] cell numbers (0 = none): orientation.
* "evidence": nine [DTL, FO] cell numbers (0 = none): the cell each phase's
  checkpoints were judged from, asked as a separate look.
* checkpoint entries [score, confidence, direction]; an _any list is led by
  its camera digit (1 DTL, 2 FO).

CaddieJ validates locate locally (prior, local order, cross-camera) and accepts
evidence only inside the locate-derived region (`hybrid_replay.py`).

Modes (exactly one paid call, ever):
  (default)   build, check neutrality, write criteria + request texts; no call
  --send      make THE call; refuses if output/poc3/reply.json exists
  --analyse   read output/poc3/reply.json; no call
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
import hybrid_replay as hy  # noqa: E402
import poc2_vx14b as poc2  # noqa: E402
import poc_vx14 as poc1  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "output" / "poc3"
REPLY = OUT / "reply.json"
CRITERIA = OUT / "criteria.json"
POS = poc1.POSITIONS
UNANCHORED_MAX_CELLS = 4

_JOB = """\
JOB 1b - "locate" and "evidence": find the swing positions yourself, before rating anything.
The camera cells are stills of ONE swing in time order, left to right, top to
bottom, each camera on its own clock. A cell's ID and time only put it in
order; nothing printed on a cell says which position it shows. Look along each
camera's whole row first. Write a cell as its number: DTL_13 is 13 in a DTL
slot, FO_13 is 13 in an FO slot; 0 means none.
"locate": nine pairs [DTL, FO], P1 to P9: the cell where that position is best
seen on each camera, or 0 when no cell shows it (it fell between stills, came
before the first or after the last, or is hidden). Never force a position.
"evidence": nine pairs [DTL, FO], P1 to P9: the cell you actually judge that
position's checkpoints from on each camera, 0 when you judge none there. This
is a second, separate look, not a copy of locate: for each position, pick the
cell that best shows what its checkpoints ask about. Often that is the locate
cell; when a neighbouring cell shows those mechanics better, give that one. It
must still show the same position, never a cell of a neighbouring position.
P1 setup: address, club behind the ball, before the club moves.
P2 takeaway: shaft about parallel to the ground going back, hands about hip high.
P3 backswing: lead arm about horizontal going back, hands about chest high,
shaft pointing up, not yet behind the head.
P4 top: hands at their highest, shaft behind the head; the next cells show the
hands lowering. During a pause, any cell of the pause.
P5 transition: lead arm about horizontal again coming down; the shaft still
points up above the hands.
P6 downswing (delivery): hands at the trail hip, shaft about horizontal and
trailing the hands, wrists still hinged.
P7 impact: the clubhead at the ball, lead arm and shaft one straight line from
the shoulder to the clubhead, hands at or just ahead of the ball. Before it the
clubhead trails the hands; after it the clubhead is past the hands. Judge by
club and hands, not by whether a ball is visible: other balls may lie on the
mat. If contact fell between two cells, give the one whose clubhead is nearer
the ball, and if tied the earlier. Search hint only: on these replays impact
is usually among the cells from about +1.7s to +2.2s; the picture decides.
P8 release: both arms straight, shaft about horizontal pointing at the target;
once the shaft is vertical it is past.
P9 finish: the balanced finish, turned to the target, club over the shoulder.
Along one camera positions never go back in time from P1 to P9. One cell may
stand for two neighbouring positions only when the swing passed both within it.

"""

_EDITS = (
    (
        "in that order: null, or [score, cell, confidence, direction]:",
        "in that order: null, or [score, confidence, direction]. An _any list starts\n"
        "with one number before its entries: the camera you judged it on, 1 DTL, 2 FO:",
    ),
    (
        '- cell: the printed ID of the ONE camera cell it was seen in, like "DTL_P5",\n'
        "  from that list's camera. Never IMPACT, never ALL_VALUES.\n",
        "",
    ),
    (
        "No sentences: CaddieJ knows each cell's camera, and takes its moment from your\n"
        '"frames" answer.',
        'No cell and no sentences: CaddieJ takes each list\'s picture from your\n'
        '"evidence" answer: a _dtl list from that position\'s DTL evidence cell, a _fo\n'
        "list from its FO evidence cell, an _any list from the camera its first number\n"
        "names. Score only what that cell shows. When that evidence is 0, the entries\n"
        "are null.",
    ),
    (
        "This is checked against what YOU said\n"
        "each picture shows in JOB 1b: a checkpoint cited from a picture you called\n"
        "unclear, from the wrong moment or from the wrong view is thrown away.",
        "CaddieJ checks that each\n"
        "evidence cell lies between your locate cells of the neighbouring positions;\n"
        "evidence from another part of the swing is thrown away with its checkpoints.",
    ),
    poc2._SYSTEM_EDITS[4],
)


def system_v14h() -> str:
    text = vx.SYSTEM
    start = text.index('JOB 1b - "frames"')
    end = text.index('JOB 2 - "checkpoints"')
    text = text[:start] + _JOB + text[end:]
    for old, new in _EDITS:
        assert old in text, old[:60]
        text = text.replace(old, new)
    return text


def evidence_v14h(rows, cues) -> str:
    text = poc2.evidence_v14b(rows, cues)
    for old, new in (
        ("Only these may be given in locate:", "Only these may be given in locate and evidence, by number:"),
        ("never in locate and never cited by a checkpoint", "never in locate or evidence"),
    ):
        assert old in text
        text = text.replace(old, new)
    return text


def schema_v14h(base: dict) -> dict:
    schema = json.loads(json.dumps(base))
    defs = {"dir": schema["$defs"]["dir"]}
    defs["entry"] = {
        "anyOf": [
            {"type": "null"},
            {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {"anyOf": [
                    {"type": "integer", "minimum": 0, "maximum": 100},
                    {"$ref": "#/$defs/dir"},
                    {"type": "null"},
                ]},
            },
        ]
    }
    grid = {
        "type": "array", "minItems": 9, "maxItems": 9,
        "items": {"type": "array", "minItems": 2, "maxItems": 2,
                  "items": {"type": "integer", "minimum": 0, "maximum": 18}},
    }
    properties = schema["properties"]
    properties.pop("frames")
    for key, items in vx.WIRE_LISTS.items():
        if key.endswith("_any"):
            properties["checkpoints"]["properties"][key] = {
                "type": "array", "minItems": len(items) + 1, "maxItems": len(items) + 1,
                "items": {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 2}, {"$ref": "#/$defs/entry"}]},
            }
        else:
            properties["checkpoints"]["properties"][key] = {
                "type": "array", "minItems": len(items), "maxItems": len(items),
                "items": {"$ref": "#/$defs/entry"},
            }
    schema["properties"] = {"locate": grid, "evidence": json.loads(json.dumps(grid)), **properties}
    schema["required"] = ["locate", "evidence", *[k for k in schema["required"] if k != "frames"]]
    schema["$defs"] = defs
    return schema


def build(rows, settings: Settings):
    cues = vx.possible_cues(ClubKind.UNKNOWN, has_metrics=True)
    image = ImageInput(data=poc1.SHEET.read_bytes(), label=SHEET_LABEL, detail="high")
    base = vx.build_request(
        evidence=evidence_v14h(rows, cues),
        images=(image,),
        cues=cues,
        max_output_tokens=settings.coaching_protee_max_output_tokens,
        timeout_seconds=settings.coach_openai_timeout,
        reasoning_effort=settings.coaching_reasoning_effort or None,
        frame_ids=tuple(r["cell_id"] for r in rows),
        body_frame_ids=tuple(r["cell_id"] for r in poc1.body(rows)),
        cell_views={r["cell_id"]: PanelView(r["view"]) for r in poc1.body(rows)},
    )
    return replace(base, system=system_v14h(), schema=schema_v14h(dict(base.schema))), cues


# --- criteria, fixed before the call --------------------------------------------

PASS_CRITERIA = {
    "locate_wrong_max": 1,
    "locate_wrong_definition": "raw LOCATE (before CaddieJ's validation), 18 slots, verdict 'wrong' under POC 1's "
                               "RULE; nulls are reported as missed, not counted as wrong",
    "p7_both_cameras_capture_index_in": [40, 41, 42],
    "p7_definition": "raw LOCATE P7 on DTL and on FO each within one captured still of true impact (#41)",
    "accepted_min": 40,
    "wrong_position_accepted_max": 3,
    "wrong_position_definition": "accepted checkpoint whose evidence still is graded 'wrong' for its phase under "
                                 "POC 1's TRUTH/RULE (neighbour of a correct still = acceptable)",
    "output_tokens_max": 1000,
    "output_tokens_note": "approximately: 1001-1100 reported as borderline, >1100 fails",
    "measured_not_criteria": ["coverage", "swing_score", "faults", "root", "cue", "input_tokens",
                              "reasoning_tokens", "wall_seconds", "strike_to_verdict", "cost"],
}


# --- reading ---------------------------------------------------------------------


def num_to_cell(cams, key, n):
    if isinstance(n, bool) or not isinstance(n, int) or n == 0:
        return None
    cells = cams[key]["cells"]
    return cells[n - 1] if 1 <= n <= len(cells) else "invalid"


def grids(payload, cams):
    out = {}
    for name in ("locate", "evidence"):
        raw = payload.get(name)
        g = {"dtl": [None] * 9, "fo": [None] * 9}
        if isinstance(raw, list) and len(raw) == 9:
            for p, pair in enumerate(raw):
                if isinstance(pair, list) and len(pair) == 2:
                    g["dtl"][p] = num_to_cell(cams, "dtl", pair[0])
                    g["fo"][p] = num_to_cell(cams, "fo", pair[1])
        out[name] = g
    return out["locate"], out["evidence"]


def evidence_region(cams, kept, key, p):
    allowed, anchored = hy.region(cams, kept, key, p)
    if not anchored and len(allowed) > UNANCHORED_MAX_CELLS:
        return set(), False
    return allowed, anchored


def judge_evidence(cams, kept, key, p, cell):
    if cell is None:
        return "not_located"
    if cell == "invalid":
        return "camera"
    allowed, _anchored = evidence_region(cams, kept, key, p)
    return "ok" if cell in allowed else "outside_region"


def checkpoint_entries(payload, evidence, cams, kept):
    """-> [(spec, named item | None | refused)], plus per-list camera use."""

    out, used = [], {}
    raw = payload.get("checkpoints") or {}
    for key, specs in vx.WIRE_LISTS.items():
        phase_name, camera = key.rsplit("_", 1)
        p = list(PHASE_ORDER).index(vx.SwingPhase(phase_name))
        entries = raw.get(key)
        if camera == "any":
            if not isinstance(entries, list) or len(entries) != len(specs) + 1 or entries[0] not in (1, 2):
                out += [(s, vx.REFUSED + "length") for s in specs]
                continue
            cam = "dtl" if entries[0] == 1 else "fo"
            entries = entries[1:]
        else:
            cam = camera
            if not isinstance(entries, list) or len(entries) != len(specs):
                out += [(s, vx.REFUSED + "length") for s in specs]
                continue
        cell = evidence[cam][p]
        verdict = judge_evidence(cams, kept, cam, p, cell)
        used[key] = (cam, cell, verdict)
        for spec, e in zip(specs, entries, strict=True):
            if e is None:
                out.append((spec, None))
            elif not isinstance(e, list) or len(e) != 3 or not all(
                isinstance(x, int) and not isinstance(x, bool) for x in e[:2]
            ):
                out.append((spec, vx.REFUSED + "shape"))
            elif verdict != "ok":
                out.append((spec, vx.REFUSED + verdict))
            else:
                out.append((spec, {"score": e[0], "conf": e[1] / 100, "cell": cell, "dir": e[2]}))
    return out, used


def grade_cell(rows, key, p, cell):
    camera_id = hy.CAMERAS[key]
    sheet = [r for r in poc1.body(rows) if r["camera_id"] == camera_id]
    ix = [r["capture_index"] for r in sheet]
    truth = poc1.TRUTH[camera_id][POS[p]]
    ok = [i for i, c in enumerate(ix) if c in truth]
    if cell is None:
        return None, "unavailable" if not ok else "missed"
    if cell == "invalid":
        return None, "invalid"
    row = next(r for r in sheet if r["cell_id"] == cell)
    at = ix.index(row["capture_index"])
    if row["capture_index"] in truth:
        return row["capture_index"], "correct"
    return row["capture_index"], "acceptable" if any(abs(at - i) == 1 for i in ok) else "wrong"


# --- the call ------------------------------------------------------------------


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
    raw_locate, evidence = grids(payload, cams)
    kept, notes = hy.validate_locate(
        {k: [None if c == "invalid" else c for c in v] for k, v in raw_locate.items()}, cams)
    entries, used = checkpoint_entries(payload, evidence, cams, kept)
    accepted, rejected, unanchored = hy.assess(entries, cams, kept)
    body_cells = [r["cell_id"] for r in poc1.body(rows)]
    cell_map = {r["cell_id"]: r["frame_id"] for r in rows}
    metrics_only = vx.decode_wire({k: v for k, v in payload.items() if k in ("metrics", "coach")}, body_cells)
    canon = vx.canonical_reply({**metrics_only, "checkpoints": {}, "frames": {}}, cell_map)
    validate_seconds = time.perf_counter() - started
    summary = hy.summary("POC3 hybrid", accepted, rejected, canon, rows, settings,
                         {"unanchored_accepts": unanchored, "rejected_list": rejected})

    locate_rows = []
    for key in ("dtl", "fo"):
        for p in range(9):
            raw_cell = raw_locate[key][p]
            ix, verdict = grade_cell(rows, key, p, raw_cell)
            locate_rows.append({"camera": key, "position": POS[p], "cell": raw_cell, "capture_index": ix,
                                "verdict": verdict, "kept": kept[key][p] is not None})
    evidence_rows = []
    for key in ("dtl", "fo"):
        for p in range(9):
            cell = evidence[key][p]
            if cell is None:
                continue
            ix, verdict = grade_cell(rows, key, p, cell)
            evidence_rows.append({
                "camera": key, "position": POS[p], "cell": cell, "capture_index": ix,
                "locate": raw_locate[key][p], "equals_locate": cell == raw_locate[key][p],
                "validator": judge_evidence(cams, kept, key, p, cell), "truth": verdict,
                "used_by": [k for k, (c, cl, _v) in used.items() if c == key and cl == cell
                            and k.rsplit("_", 1)[0] == PHASE_ORDER[p].value],
            })
    eq = [e for e in evidence_rows if e["equals_locate"]]
    diff = [e for e in evidence_rows if not e["equals_locate"]]
    usage = record.get("usage") or {}
    p7 = {k: next(r["capture_index"] for r in locate_rows if r["camera"] == k and r["position"] == "P7")
          for k in ("dtl", "fo")}
    criteria = json.loads(CRITERIA.read_text(encoding="utf-8"))["criteria"]
    locate_wrong = sum(r["verdict"] == "wrong" for r in locate_rows)
    wrong_pos = len(summary["accepted_wrong_position"])
    out_tokens = usage.get("output_tokens") or 0
    verdicts = {
        "locate_wrong": [locate_wrong, locate_wrong <= criteria["locate_wrong_max"]],
        "p7": [p7, all(v in criteria["p7_both_cameras_capture_index_in"] for v in p7.values())],
        "accepted": [summary["accepted"], summary["accepted"] >= criteria["accepted_min"]],
        "wrong_position_accepted": [wrong_pos, wrong_pos <= criteria["wrong_position_accepted_max"]],
        "output_tokens": [out_tokens, "pass" if out_tokens <= 1000 else "borderline" if out_tokens <= 1100 else "fail"],
    }
    result = {
        "raw_locate": payload.get("locate"), "raw_evidence": payload.get("evidence"),
        "any_cameras": {k: (payload.get("checkpoints") or {}).get(k, [None])[0] for k in vx.WIRE_LISTS if k.endswith("_any")},
        "locate_notes": notes,
        "locate": locate_rows,
        "evidence": evidence_rows,
        "evidence_counts": {
            "entries": len(evidence_rows),
            "equal_to_locate": len(eq),
            "different_and_validator_ok": sum(e["validator"] == "ok" for e in diff),
            "different_and_validator_rejected": sum(e["validator"] != "ok" for e in diff),
            "different_and_truth_ok": sum(e["truth"] in ("correct", "acceptable") for e in diff),
            "different_and_truth_wrong": sum(e["truth"] == "wrong" for e in diff),
        },
        "list_use": {k: list(v) for k, v in used.items()},
        "summary": summary,
        "usage": usage,
        "cost_usd": estimate_cost(record["model"], usage.get("input_tokens"), usage.get("output_tokens")),
        "wall_seconds": record["wall_seconds"],
        "validate_seconds": round(validate_seconds, 3),
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
        CRITERIA.write_text(json.dumps({"written_at": datetime.now(UTC).isoformat(),
                                        "sheet": poc1.SHEET.name, "criteria": PASS_CRITERIA}, indent=1),
                            encoding="utf-8")
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
