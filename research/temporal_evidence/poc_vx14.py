"""Temporal POC: one vx14-draft request on one saved shot, then CaddieJ's own reading of it.

Research only. Production code is imported read-only and never modified:
the vx14 draft is vx13's prompt and schema with JOB 1b ("frames": one phase per
cell) replaced by LOCATE ("locate": one cell per phase per camera), built here
by text substitution and schema surgery on what `caddiej.coaching.vx` returns.

The sheet is the 18-per-camera temporal sheet that `temporal_sheets.py` drew for
shot 20261008T092949-4-d05a29c4: neutral IDs (DTL_01..18, FO_01..18) and
"+s.ss" captions only. Nothing the model receives carries a phase word next
to a cell, the old calibrated aims, or the ground truth below.

Modes (exactly one paid call, ever):
  (default)   build and check the request, print its breakdown; no call
  --send      make THE call; refuses if output/poc/reply.json exists
  --analyse   read output/poc/reply.json through CaddieJ's parsers; no call

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\poc_vx14.py [--send|--analyse]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching import vx
from caddiej.coaching.baseline import Baseline
from caddiej.coaching.benchmark import MeteredOpenAIProvider, request_breakdown
from caddiej.coaching.cues import admissible_cues, authority_cue, cue_supported
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.lesson import LessonStep, LessonTracker
from caddiej.coaching.provider import ImageInput
from caddiej.coaching.score import score_swing
from caddiej.coaching.service import _needs_reconciling, _protee_signs
from caddiej.coaching.session import SessionProgress
from caddiej.coaching.swing_faults import corroborate, infer_faults, rank_root_cause
from caddiej.coaching.validate import validate
from caddiej.config import Settings
from caddiej.domain.coaching import SEVERITY_FINE, AdviceKind, CoachingFocus, Diagnosis
from caddiej.domain.cues import PanelView
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.statistics import club_of
from caddiej.domain.swing_phase import PHASE_ORDER
from caddiej.swing_capture.sheet import SHEET_LABEL, CellKind, SheetCell, layout_sheet
from caddiej.swing_capture.store import METRICS_FRAME_ID, load_capture

ROOT = Path(__file__).resolve().parents[2]
SHOT = ROOT / "media" / "swing_shots" / "20261008T092949-4-d05a29c4"
HERE = Path(__file__).parent
SHEET = HERE / "output" / "sheets" / f"{SHOT.name}_T36.jpg"
PROVENANCE = HERE / "output" / "sheets" / f"{SHOT.name}_T36.json"
OUT = HERE / "output" / "poc"
REPLY = OUT / "reply.json"

PROMPT_VERSION = "vx14-draft"
POSITIONS = tuple(f"P{n}" for n in range(1, len(PHASE_ORDER) + 1))

# --- ground truth: fixed BEFORE the call, never sent ---------------------------
# Capture indices judged by eye on the full-resolution diagnostic originals
# (the same sets as temporal_sheets.TRUTH, written before this POC existed).
_EARLY = {"P1": {15, 16, 17}, "P2": {19, 20, 21}, "P3": {22, 23, 24}, "P9": {48, 49, 50, 51}}
TRUTH = {
    "swing_behind": {**_EARLY, "P4": set(range(28, 37)), "P5": {38, 39}, "P6": {39, 40}, "P7": {41}, "P8": {42, 43}},
    "swing_side": {**_EARLY, "P4": set(range(30, 36)), "P5": {37, 38, 39}, "P6": {39, 40}, "P7": {41}, "P8": {42, 43}},
}
RULE = (
    "correct: the cell's original is in the accepted set; acceptable: the cell is the "
    "sheet neighbour of a correct cell (the position fell between two kept stills); "
    "wrong: anything else; null when a correct cell existed on the sheet: missed"
)


# --- the evidence ---------------------------------------------------------------


def load_cells() -> list[dict]:
    rows = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    for row in rows:
        if row["cell_id"] == "ALL_VALUES":
            row["frame_id"] = METRICS_FRAME_ID
        elif "capture_index" in row:
            row["frame_id"] = f"{row['camera_id']}_c{row['capture_index']:02d}"
    return rows


def body(rows: list[dict]) -> list[dict]:
    return [row for row in rows if "capture_index" in row]


# --- the vx14 draft: vx13 with LOCATE in place of JOB 1b -----------------------

_LOCATE_JOB = """\
JOB 1b - "locate": find the swing positions yourself, before rating anything.
The camera cells are stills of ONE swing in time order, left to right, top to
bottom, each camera on its own clock. A cell's ID and time only put it in
order; nothing printed on a cell says which moment it shows. For each camera,
"dtl" from DTL_ cells and "fo" from FO_ cells, give nine entries in this
order: setup (P1 address), takeaway (P2 shaft parallel to the ground going
back), backswing (P3 lead arm parallel going back), top (P4), transition (P5
lead arm about parallel coming down), downswing (P6 delivery, shaft near
parallel coming down), impact (P7 club at the ball), release (P8 just after
the ball, shaft parallel, arms extending), finish (P9).
Each entry is the ID of the ONE cell of that camera that shows that position
best, or null when no cell shows it closely enough: it fell between two
stills, came before the first or after the last, or is hidden.
The positions happen in this order, so along one camera's nine entries the
cells never go back in time. One cell may stand for two neighbouring positions
only when the swing really passed both within it, like a top with no pause.
Look along the whole row of a camera first, then choose. Null is better than
a cell that only nearly shows the position.

"""

_SYSTEM_EDITS = (
    (
        '- cell: the printed ID of the ONE camera cell it was seen in, like "DTL_P5",\n'
        "  from that list's camera. Never IMPACT, never ALL_VALUES.",
        '- cell: the ID of the cell you gave in "locate" for one of this checkpoint\'s\n'
        '  moments on that list\'s camera, like "DTL_07". Never IMPACT, never ALL_VALUES.',
    ),
    (
        "No sentences: CaddieJ knows each cell's camera, and takes its moment from your\n"
        '"frames" answer.',
        "No sentences: CaddieJ knows each cell's camera, and takes its moment from your\n"
        '"locate" answer.',
    ),
    (
        "This is checked against what YOU said\n"
        "each picture shows in JOB 1b: a checkpoint cited from a picture you called\n"
        "unclear, from the wrong moment or from the wrong view is thrown away.",
        'This is checked against your "locate"\n'
        "answer: a checkpoint that cites a cell you did not locate as one of its\n"
        "moments, or a cell from the wrong view, is thrown away.",
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


def system_v14() -> str:
    text = vx.SYSTEM
    start = text.index('JOB 1b - "frames"')
    end = text.index('JOB 2 - "checkpoints"')
    text = text[:start] + _LOCATE_JOB + text[end:]
    for old, new in _SYSTEM_EDITS:
        assert old in text, old[:60]
        text = text.replace(old, new)
    return text


def evidence_v14(rows: list[dict], cues) -> str:
    cell_views = {row["cell_id"]: PanelView(row["view"]) for row in body(rows)}
    text = vx.render_evidence(
        club=ClubKind.UNKNOWN,
        handedness=Handedness.UNKNOWN,
        has_metrics=True,
        swing_images=tuple(row["cell_id"] for row in body(rows)),
        cues=cues,
        session_focus=None,
        focus_shots=0,
        frames=tuple((row["cell_id"], row["cell_id"]) for row in rows),
        sheet=True,
        cell_views=cell_views,
    )
    old_sheet = "with CaddieJ's ~moment and that camera's time."
    assert old_sheet in text
    text = text.replace(
        old_sheet, "with that camera's time, +seconds from the start of its replay."
    )
    lines = text.split("\n")
    head = "Camera cells, each a key of frames and the only cells a checkpoint may cite, as ID ~aimed moment: "
    index = next(i for i, line in enumerate(lines) if line.startswith(head))
    timed = ", ".join(
        f"{row['cell_id']} +{row['offset_from_replay_start_seconds']:.2f}s" for row in body(rows)
    )
    lines[index] = (
        "Camera cells in time order, as ID and +seconds from the start of that "
        "camera's replay; DTL and FO are separate clocks, and a time names no "
        f"moment. Only these may be given in locate or cited by a checkpoint: {timed}."
    )
    text = "\n".join(lines)
    old_other = "never a key of frames"
    assert old_other in text
    return text.replace(old_other, "never in locate")


def locate_schema(rows: list[dict]) -> dict:
    def camera(view: str) -> dict:
        cells = [row["cell_id"] for row in body(rows) if row["view"] == view]
        return {
            "type": "array",
            "minItems": len(POSITIONS),
            "maxItems": len(POSITIONS),
            "items": {"anyOf": [{"type": "null"}, {"type": "string", "enum": cells}]},
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["dtl", "fo"],
        "properties": {
            "dtl": camera(PanelView.BEHIND.value),
            "fo": camera(PanelView.SIDE.value),
        },
    }


def build(rows: list[dict], settings: Settings):
    cues = vx.possible_cues(ClubKind.UNKNOWN, has_metrics=True)
    cell_ids = tuple(row["cell_id"] for row in rows)
    body_ids = tuple(row["cell_id"] for row in body(rows))
    cell_views = {row["cell_id"]: PanelView(row["view"]) for row in body(rows)}
    image = ImageInput(data=SHEET.read_bytes(), label=SHEET_LABEL, detail="high")
    base = vx.build_request(
        evidence=evidence_v14(rows, cues),
        images=(image,),
        cues=cues,
        max_output_tokens=settings.coaching_protee_max_output_tokens,
        timeout_seconds=settings.coach_openai_timeout,
        reasoning_effort=settings.coaching_reasoning_effort or None,
        frame_ids=cell_ids,
        body_frame_ids=body_ids,
        cell_views=cell_views,
    )
    schema = json.loads(json.dumps(base.schema))
    properties = schema["properties"]
    properties.pop("frames")
    schema["properties"] = {"locate": locate_schema(rows), **properties}
    schema["required"] = ["locate", *[key for key in schema["required"] if key != "frames"]]
    schema["$defs"].pop("phase", None)
    request = replace(base, system=system_v14(), schema=schema)
    return request, cues


def check_neutral(request) -> list[str]:
    """Nothing in the request may tie a cell to a moment, an aim or the truth."""

    problems = []
    sent = request.system + "\n" + request.evidence + "\n" + json.dumps(request.schema)
    for needle in ("~setup", "~takeaway", "~backswing", "~top", "~transition", "~downswing",
                   "~impact", "~release", "~finish", "DTL_P", "FO_P", "@1.", "@2.", "calibrated"):
        if needle in sent:
            problems.append(needle)
    return problems


# --- reading the reply ---------------------------------------------------------


def validate_locate(raw: object, rows: list[dict]) -> tuple[dict[str, list[str | None]], list[str]]:
    """Existence and camera, chronology, at most two neighbouring positions per cell."""

    notes: list[str] = []
    order = {row["cell_id"]: row["offset_from_replay_start_seconds"] for row in body(rows)}
    views = {row["cell_id"]: row["view"] for row in body(rows)}
    out: dict[str, list[str | None]] = {}
    for key, view in (("dtl", PanelView.BEHIND.value), ("fo", PanelView.SIDE.value)):
        entries = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(entries, list) or len(entries) != len(POSITIONS):
            notes.append(f"{key}: locate_length")
            out[key] = [None] * len(POSITIONS)
            continue
        clean: list[str | None] = []
        for position, cell in zip(POSITIONS, entries, strict=True):
            if cell is not None and (cell not in views or views[cell] != view):
                notes.append(f"{key} {position}: locate_cell {cell}")
                cell = None
            clean.append(cell)
        times = [order[c] for c in clean if c is not None]
        if any(b < a for a, b in zip(times, times[1:])):
            notes.append(f"{key}: locate_order -- whole camera dropped")
            clean = [None] * len(POSITIONS)
        for cell in set(c for c in clean if c is not None):
            at = [i for i, c in enumerate(clean) if c == cell]
            if len(at) > 2 or (len(at) == 2 and at[1] - at[0] != 1):
                notes.append(f"{key}: locate_shared {cell} at {[POSITIONS[i] for i in at]}")
                for i in at:
                    clean[i] = None
        out[key] = clean
    return out, notes


def invert(located: dict[str, list[str | None]]) -> tuple[dict[str, str], dict[str, str]]:
    """cell -> phase, and a second map for a cell standing for two positions."""

    first: dict[str, str] = {}
    second: dict[str, str] = {}
    for entries in located.values():
        for phase, cell in zip(PHASE_ORDER, entries, strict=True):
            if cell is None:
                continue
            if cell in first:
                second[cell] = phase.value
            else:
                first[cell] = phase.value
    return first, second


def read_checkpoints(payload: dict, rows: list[dict], located: dict[str, list[str | None]]):
    cell_map = {row["cell_id"]: row["frame_id"] for row in rows}
    body_cells = [row["cell_id"] for row in body(rows)]
    frame_views = {row["frame_id"]: PanelView(row["view"]) for row in body(rows)}
    body_frames = tuple(row["frame_id"] for row in body(rows))
    first, second = invert(located)
    rest = {key: value for key, value in payload.items() if key != "locate"}
    named = vx.decode_wire(rest, body_cells)

    def parse(frames: dict[str, str]):
        canon = vx.canonical_reply({**named, "frames": frames}, cell_map)
        phases = vx.parse_frames(canon["frames"])
        return canon, vx.parse_checkpoints(
            canon.get("checkpoints"), body_frames=body_frames, frame_phases=phases, frame_views=frame_views
        )

    canon, (assessments, rejected) = parse(first)
    if second:
        _, (again, _rej) = parse({**first, **second})
        refused_phase = {key for key, reason in rejected if reason == "phase"}
        rescued = [a for a in again if a.checkpoint.value in refused_phase]
        if rescued:
            keep = {a.checkpoint for a in rescued}
            assessments = tuple(sorted((*assessments, *rescued), key=lambda a: list(vx.CHECKPOINTS_BY_ID).index(a.checkpoint)))
            rejected = tuple(r for r in rejected if not (r[1] == "phase" and vx.CheckpointId(r[0]) in keep))
    return canon, assessments, rejected


def grade(located: dict[str, list[str | None]], rows: list[dict]) -> list[dict]:
    by_cell = {row["cell_id"]: row for row in body(rows)}
    table = []
    for key, camera_id in (("dtl", "swing_behind"), ("fo", "swing_side")):
        sheet = [row for row in body(rows) if row["camera_id"] == camera_id]
        indices = [row["capture_index"] for row in sheet]
        for position, cell in zip(POSITIONS, located[key], strict=True):
            accepted = TRUTH[camera_id][position]
            correct_cells = [i for i, ix in enumerate(indices) if ix in accepted]
            if cell is None:
                verdict = "missed" if correct_cells else "null (no correct cell on the sheet)"
                row = None
            else:
                row = by_cell[cell]
                at = indices.index(row["capture_index"])
                if row["capture_index"] in accepted:
                    verdict = "correct"
                elif any(abs(at - c) == 1 for c in correct_cells):
                    verdict = "acceptable"
                else:
                    verdict = "wrong"
            table.append({
                "camera": key, "position": position, "cell": cell,
                "capture_index": None if row is None else row["capture_index"],
                "original": None if row is None else row["diagnostic_file"],
                "t": None if row is None else row["offset_from_replay_start_seconds"],
                "accepted": sorted(accepted),
                "accepted_cells": [sheet[i]["cell_id"] for i in correct_cells],
                "verdict": verdict,
            })
    return table


def conclude(canon: dict, assessments, settings: Settings) -> dict:
    """The live path's `_conclude_protee`, in order, on a fresh session."""

    capture = load_capture(SHOT)
    now = datetime.now(UTC)
    metrics = vx.parse_metrics(canon.get("metrics"))
    swing_score = score_swing(metrics, club=None, handedness=Handedness.UNKNOWN, checkpoints=assessments)
    mechanics = swing_score.mechanics
    diagnosis = Diagnosis(
        shot_id=uuid4(), focus=CoachingFocus.NONE, severity=0.0, display="", speech="",
        club=ClubKind.UNKNOWN, handedness=Handedness.UNKNOWN, baseline_shots=0,
    )
    shot = vx.shot_from_metrics(
        metrics, shot_id=diagnosis.shot_id, club=None, handedness=Handedness.UNKNOWN,
        observed_at=now, observed_monotonic=time.monotonic(),
    )
    if shot is not None:
        diagnosis = diagnose(shot, Baseline().for_club(club_of(shot)), None, SEVERITY_FINE, SessionProgress())
    allowed = ()
    if diagnosis.kind is not AdviceKind.NONE:
        allowed = admissible_cues(diagnosis, has_frames=True, capture=capture, visual_cues=False, fine=SEVERITY_FINE)
    signs = _protee_signs(metrics, diagnosis)
    faults = corroborate(infer_faults(mechanics), signs)
    root = rank_root_cause(faults)
    visual = settings.coaching_visual_cues
    step = LessonStep(progress=None) if mechanics is None or not visual else LessonTracker().step(mechanics, faults, root)
    if step.cue is not None and visual and cue_supported(step.cue, capture):
        allowed = (*allowed, step.cue)
    authority = authority_cue(diagnosis, allowed, step, root)
    coach = vx.coach_payload(canon)
    outcome: str
    spoken = None
    if coach is None or coach.get("kind") == AdviceKind.NONE.value or not coach.get("cue"):
        outcome = "model_declined -> CaddieJ fallback" + (f" (lesson {authority.cue.value})" if authority else "")
    elif _needs_reconciling(str(coach.get("cue")), diagnosis, allowed, authority):
        outcome = f"reconciled -> CaddieJ's lesson {authority.cue.value if authority else diagnosis.cue}"
    else:
        try:
            advice = validate(coach, diagnosis, now, frames_sent=True, fine=SEVERITY_FINE,
                              frame_ids=tuple(r for r in canon_frame_ids(canon)))
            if advice.cue is None or advice.cue not in allowed:
                outcome = f"refused: cue {advice.cue} not supported by the numbers read"
            else:
                outcome = "accepted"
                spoken = advice.speech
        except Exception as error:  # SpeechRejected / AdviceRejected
            outcome = f"refused: {type(error).__name__}: {error}"
    return {
        "metrics_read": len(metrics.available),
        "metrics_rejected": list(metrics.rejected),
        "metrics": {name: (None if m is None else [m.value, getattr(m.unit, 'value', m.unit), getattr(m.direction, 'value', m.direction)])
                    for name, m in metrics.values.items()} if hasattr(metrics, "values") else None,
        "diagnosis": {"kind": diagnosis.kind.value, "focus": diagnosis.focus.value,
                      "cue": None if diagnosis.cue is None else diagnosis.cue.value, "severity": round(diagnosis.severity, 3)},
        "admissible": [c.value for c in allowed],
        "swing_score": swing_score.value,
        "delivery_value": getattr(swing_score, "delivery_value", None),
        "mechanics": None if mechanics is None else {
            "value": getattr(mechanics, "value", None),
            "phases": {str(k): v for k, v in (getattr(mechanics, "phases", {}) or {}).items()} if isinstance(getattr(mechanics, "phases", None), dict) else str(getattr(mechanics, "phases", ""))[:400],
        },
        "faults": [{"fault": f.fault.value, "severity": round(f.severity, 2), "confidence": round(f.confidence, 2)} for f in faults],
        "root": None if root is None else str(root)[:600],
        "lesson_cue": None if step.cue is None else step.cue.value,
        "authority": None if authority is None else {"cue": authority.cue.value, "from_visual": authority.from_visual},
        "model_coach": coach,
        "outcome": outcome,
        "spoken": spoken,
    }


def canon_frame_ids(canon: dict) -> list[str]:
    rows = load_cells()
    return [row["frame_id"] for row in rows]


# --- main ----------------------------------------------------------------------


def render_seconds(rows: list[dict]) -> float:
    """Time to draw this sheet in memory from the originals, as production would."""

    cells = []
    for row in rows:
        if "capture_index" in row:
            path = SHOT / row["diagnostic_file"]
            kind, view = CellKind.BODY, PanelView(row["view"])
        else:
            path = SHOT / row["file"]
            kind = CellKind.METRICS if row["cell_id"] == "ALL_VALUES" else CellKind.PANEL
            view = PanelView.NONE
        with Image.open(path) as im:
            w, h = im.size
        cells.append((SheetCell(row["cell_id"], row["frame_id"] if "frame_id" in row else row["cell_id"], path, kind, row.get("camera_id", "x"), view), w, h))
    started = time.perf_counter()
    layout = layout_sheet(cells, 2048)
    canvas = Image.new("RGB", (layout.width, layout.height), (24, 24, 24))
    for cell, _w, _h in cells:
        place = layout.placement(cell.cell_id)
        with Image.open(cell.path) as im:
            pic = im.convert("RGB").resize((place.image.width, place.image.height), Image.Resampling.LANCZOS)
        canvas.paste(pic, (place.image.x, place.image.y))
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=90, subsampling=0)
    return time.perf_counter() - started


async def send(request, settings: Settings) -> None:
    if REPLY.exists():
        raise SystemExit(f"{REPLY} exists: the one call has been made. Use --analyse.")
    OUT.mkdir(parents=True, exist_ok=True)
    provider = MeteredOpenAIProvider(
        api_key=settings.coaching_api_key.get_secret_value(),
        model=settings.coaching_model,
        base_url=settings.coaching_api_base or None,
    )
    REPLY.write_text(json.dumps({"status": "in_flight", "started_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    started = time.perf_counter()
    error = None
    reply = None
    try:
        reply = await provider.complete(request)
    except Exception as exc:  # recorded, never retried
        error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - started
    usage = provider.usage()
    await provider.aclose()
    record = {
        "status": "ok" if reply is not None else "error",
        "error": error,
        "model": settings.coaching_model,
        "reasoning_effort": settings.coaching_reasoning_effort or None,
        "max_output_tokens": request.max_output_tokens,
        "timeout_seconds": request.timeout_seconds,
        "wall_seconds": round(wall, 3),
        "provider_latency_seconds": None if reply is None else round(reply.latency_seconds, 3),
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
    rows = load_cells()
    t0 = time.perf_counter()
    request, cues = build(rows, settings)
    build_seconds = time.perf_counter() - t0
    problems = check_neutral(request)
    if problems:
        raise SystemExit(f"request is not neutral: {problems}")
    if not options.send and not options.analyse:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "request_system.txt").write_text(request.system, encoding="utf-8")
        (OUT / "request_evidence.txt").write_text(request.evidence, encoding="utf-8")
        (OUT / "request_schema.json").write_text(json.dumps(request.schema, indent=1), encoding="utf-8")
        breakdown = request_breakdown(request, model=settings.coaching_model)
        with Image.open(SHEET) as im:
            size = im.size
        print(f"model {settings.coaching_model} effort {settings.coaching_reasoning_effort or None} "
              f"max_out {request.max_output_tokens} timeout {request.timeout_seconds}s")
        print(f"sheet {size[0]}x{size[1]} {SHEET.stat().st_size} B")
        for text in breakdown.texts:
            print(f"  {text.part:<20} {text.characters:6d} chars ~{text.approx_tokens} tok")
        print(f"  image tokens (estimate) {breakdown.image_tokens}; text ~{breakdown.text_tokens}; total ~{breakdown.total_tokens}")
        print(f"cues offered {len(cues)}; neutral check passed; build {build_seconds * 1000:.0f} ms; render {render_seconds(rows):.2f} s")
        return
    if options.send:
        asyncio.run(send(request, settings))
        return
    record = json.loads(REPLY.read_text(encoding="utf-8"))
    payload = record["payload"]
    located, notes = validate_locate(payload.get("locate"), rows)
    t1 = time.perf_counter()
    canon, assessments, rejected = read_checkpoints(payload, rows, located)
    concluded = conclude(canon, assessments, settings)
    conclude_seconds = time.perf_counter() - t1
    table = grade(located, rows)
    usage = record.get("usage") or {}
    result = {
        "rule": RULE,
        "raw_locate": payload.get("locate"),
        "locate_notes": notes,
        "localisation": table,
        "accepted_checkpoints": [
            {"checkpoint": a.checkpoint.value, "score": a.score, "confidence": a.confidence,
             "frame": a.frame_id, "phase": a.phase.value, "view": a.view.value,
             "deviation": None if a.deviation is None else a.deviation.value}
            for a in assessments
        ],
        "rejected_checkpoints": [list(r) for r in rejected],
        "scoreable_positions": sum(len(v) for v in vx.WIRE_LISTS.values()),
        "non_null_entries": sum(1 for v in (payload.get("checkpoints") or {}).values() for e in (v or []) if e is not None),
        "concluded": concluded,
        "usage": usage,
        "cost_usd": estimate_cost(record["model"], usage.get("input_tokens"), usage.get("output_tokens")),
        "wall_seconds": record["wall_seconds"],
        "conclude_seconds": round(conclude_seconds, 3),
    }
    (OUT / "analysis.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
