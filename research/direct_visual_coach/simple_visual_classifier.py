"""Simplified visual fault classifier. Research only.

Arm B of one controlled comparison. Arm A remains the frozen Catalog V1
classifier, including its DTL_LARGE camera-budget result. This module does
not change catalog definitions, coaching, primary selection, or that result.

The frames are the deterministic DTL_LARGE selection: Down-the-Line only,
24 images when that many moments exist. The model sees id, name,
visual_definition, and preferred_views. The Catalog V1 evidence rules are
not sent and are not applied to the reply.

Nothing is sent unless --send is given.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\simple_visual_classifier.py <shot-id>
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\simple_visual_classifier.py <shot-id> --send
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from typing import Any

import direct_visual_test as coach
import experiment_runner as matrix
import fault_catalog as catalog
from PIL import Image, ImageDraw, ImageFont

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching.benchmark import request_breakdown
from caddiej.coaching.provider import AdviceRequest, ImageInput

CAMERA_MODE = "DTL"
IMAGE_BUDGET = "LARGE"
SCHEMA_NAME = "simple_visual_classifier"
SHEET_COLUMNS = 4
_SHEET_MARGIN = 40
_SHEET_GAP = 32
_SHEET_LABEL_H = 64
RESULTS = Path(__file__).resolve().parent / "results" / "simple_visual_classifier"
SENT_FIELDS = ("id", "name", "visual_definition", "preferred_views")
WITHHELD_FIELDS = (
    "family",
    "required_evidence",
    "insufficient_evidence",
    "allowed_temporal_states",
    "baseline_temporal_states",
    "requires_baseline",
    "min_fault_observations",
    "must_include_states",
    "requires_handedness",
    "confusable_with",
    "mutually_exclusive_with",
    "upstream_candidates",
    "typical_consequences",
    "coaching",
)
_STATE_RANK = {
    state: index
    for index, state in enumerate(catalog.TEMPORAL_STATES)
    if state != "uncertain"
}

SYSTEM = """\
You are a visual golf swing classifier.

Study the chronological Down-the-Line images of one golf swing.

Identify which faults from the supplied CaddieJ Visual Fault Catalog are clearly visible in this swing.

For each detected fault:
- return the catalog fault_id
- confidence from 0 to 1
- cite the capture ticks that best show it
- briefly describe the visible evidence

Use the visual_definition to understand each fault.

Do not invent faults that are not visibly supported.
Do not infer an upstream cause merely because a downstream effect is visible.
Do not return a fault simply because it is common.
If no catalog fault is clearly visible, return no_proven_fault.

You are classifying visible mechanics, not coaching the golfer.
"""

INSTRUCTIONS = """\
Fill the JSON object and nothing else.

Only Down-the-Line frames are attached. No Face-On frame is attached. Do not assume a Face-On view exists.

Capture ticks increase through the swing. A higher capture_tick is later. A lower capture_tick is earlier.

TEMPORAL ORIENTATION

temporal_orientation is diagnostic context. It records the visible direction of the swing. It does not decide which faults may be returned.

temporal_orientation has exactly one object for every capture_tick you were shown, in increasing tick order, and no others.

state is exactly one of:
- setup: the golfer is in place and the swing has not started
- moving_away: the hands or club are moving away from the ball
- direction_change_region: the motion is changing from away to toward the ball, and that change is visible from these frames
- moving_toward_ball: the hands or club are moving toward the ball
- after_strike: the motion has continued past the ball
- uncertain: these frames do not show the direction

Read the states in that order. A later tick may stay in the same state or move to a later state. It must not go back to an earlier state. uncertain may sit on any tick and does not force the ticks around it.
Include direction_change_region only when the change of direction is visible. Leave it out when the frames do not show the change.
visual_basis is a short description of what is visible that establishes the direction. confidence is from 0 to 1.

A fault may cite whichever supplied ticks visually demonstrate that fault's visual_definition.

HANDEDNESS

handedness.side is exactly one of right, left, or uncertain.
right means a right-handed golfer: the lead side is the golfer's left, closer to the target.
left means a left-handed golfer: the lead side is the golfer's right, closer to the target.
uncertain means the frames do not show which side is closer to the target.
visual_basis is what is visible that establishes the side. confidence is from 0 to 1.

You may use a visually established side when a visual_definition says lead or trail.
If side is uncertain, do not fabricate a lead or trail observation.

CLASSIFICATION

status is "classified" when one or more catalog faults are clearly visible, and "no_proven_fault" when none are.
fault_candidates is empty when status is no_proven_fault.

When a fault is clearly visible, return:
- fault_id copied from the catalog
- confidence from 0 to 1
- evidence: one or more items, each with a capture_tick you were shown and a visual_basis describing what is visible there

There is no minimum number of faults. Zero faults is a complete answer.
Do not add a lesson, a drill, a feel, a floor cue, a cause chain, or a fault name that is not in the catalog.
"""


def simplified_catalog() -> list[dict[str, object]]:
    """Read-only projection. The catalog objects are not changed."""

    return [
        {
            "id": fault.id,
            "name": fault.name,
            "visual_definition": fault.visual_definition,
            "preferred_views": list(fault.preferred_views),
        }
        for fault in catalog.CATALOG
    ]


def simplified_catalog_json() -> str:
    return json.dumps(simplified_catalog(), separators=(",", ":"))


def select_dtl_large(meta: Mapping[str, Any], shot: Path) -> coach.Plan:
    """The camera-budget DTL_LARGE selection. Ticks are not hard-coded."""

    return coach.plan_selection(
        meta,
        count=matrix.moment_budget(CAMERA_MODE, IMAGE_BUDGET),
        shot=shot,
        views=matrix.views_for(CAMERA_MODE),
    )


def schema() -> dict[str, Any]:
    fault_ids = [fault.id for fault in catalog.CATALOG]
    orientation_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "capture_tick": {"type": "integer"},
            "state": {"type": "string", "enum": list(catalog.TEMPORAL_STATES)},
            "confidence": {"type": "number"},
            "visual_basis": {"type": "string"},
        },
        "required": ["capture_tick", "state", "confidence", "visual_basis"],
    }
    evidence_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "capture_tick": {"type": "integer"},
            "visual_basis": {"type": "string"},
        },
        "required": ["capture_tick", "visual_basis"],
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fault_id": {"type": "string", "enum": fault_ids},
            "confidence": {"type": "number"},
            "evidence": {"type": "array", "minItems": 1, "items": evidence_item},
        },
        "required": ["fault_id", "confidence", "evidence"],
    }
    handedness = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "side": {"type": "string", "enum": ["right", "left", "uncertain"]},
            "confidence": {"type": "number"},
            "visual_basis": {"type": "string"},
        },
        "required": ["side", "confidence", "visual_basis"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["classified", "no_proven_fault"]},
            "handedness": handedness,
            "temporal_orientation": {"type": "array", "items": orientation_item},
            "fault_candidates": {"type": "array", "items": candidate},
        },
        "required": [
            "status",
            "handedness",
            "temporal_orientation",
            "fault_candidates",
        ],
    }


def build_request(
    moments: Sequence[coach.Moment], loaded: Sequence[coach.LoadedFrame]
) -> AdviceRequest:
    """DTL frames plus the four-field catalog. Catalog V1 rules are omitted."""

    views = {frame.view for moment in moments for frame in moment.frames}
    if views != {"DTL"}:
        raise ValueError(f"this experiment sends DTL only, found {sorted(views)}")
    total = len(moments)
    image_count = sum(len(moment.frames) for moment in moments)
    lines = [
        "No launch-monitor image and no launch-monitor numbers are attached.",
        "",
        (
            f"The images are {image_count} separate full-resolution "
            "Down-the-Line frames. No Face-On frame is attached."
        ),
        (
            "They are listed from earliest to latest. "
            "capture_tick increases through the swing: a larger tick is later, "
            "a smaller tick is earlier."
        ),
        "DTL is Swing Behind (down the line). Do not assume a Face-On view exists.",
        "temporal_orientation must contain each of these capture ticks once.",
        "",
    ]
    for ordinal, moment in enumerate(moments, start=1):
        lines.append(
            f"Moment {ordinal} of {total}: capture_tick {moment.capture_tick}, "
            f"{moment.replay_seconds:.3f}s from replay start."
        )
    lines.extend(
        [
            "",
            "VISUAL FAULT CATALOG",
            "Each object is one existing catalog fault. "
            "The fields are id, name, visual_definition, and preferred_views. "
            "preferred_views names the camera that usually shows the fault. "
            "Only Down-the-Line frames are attached. "
            "Judge each visual_definition from those frames.",
            simplified_catalog_json(),
        ]
    )
    images = []
    for ordinal, item in coach._indexed(moments, loaded):
        if item.frame.view != "DTL":
            raise ValueError("a non-DTL frame reached the request")
        images.append(
            ImageInput(
                data=item.data,
                label=(
                    f"DTL capture_tick {item.frame.capture_tick}, "
                    f"Down-the-Line, moment {ordinal} of {total}"
                ),
                detail=coach.DETAIL,
            )
        )
    return AdviceRequest(
        system=SYSTEM,
        instructions=INSTRUCTIONS,
        evidence="\n".join(lines),
        schema=schema(),
        images=tuple(images),
        max_output_tokens=coach.MAX_OUTPUT_TOKENS,
        timeout_seconds=coach.TIMEOUT_SECONDS,
        schema_name=SCHEMA_NAME,
        reasoning_effort=coach.REASONING_EFFORT,
    )


def write_review_sheet(path: Path, loaded: Sequence[coach.LoadedFrame]) -> None:
    """Four-column contact sheet of the exact frames, at their original size.

    Labels sit above each frame. The picture itself is pasted unchanged.
    """

    if not loaded:
        raise ValueError("review sheet has no frames")
    ordered = sorted(loaded, key=lambda item: item.frame.capture_tick)
    images: list[tuple[int, Image.Image]] = []
    try:
        for item in ordered:
            if item.frame.view != "DTL":
                raise ValueError("review sheet frame is not DTL")
            opened = Image.open(BytesIO(item.data))
            opened.load()
            rgb = opened.convert("RGB")
            if rgb is not opened:
                opened.close()
            images.append((item.frame.capture_tick, rgb))
        _save_review_grid(path, images)
    finally:
        for _tick, image in images:
            image.close()


def _save_review_grid(path: Path, frames: Sequence[tuple[int, Image.Image]]) -> None:
    columns = SHEET_COLUMNS
    row_count = (len(frames) + columns - 1) // columns
    cell_w = max(image.width for _tick, image in frames)
    cell_h = max(image.height for _tick, image in frames)
    row_h = _SHEET_LABEL_H + cell_h
    width = _SHEET_MARGIN * 2 + columns * cell_w + (columns - 1) * _SHEET_GAP
    height = _SHEET_MARGIN * 2 + row_count * row_h + max(0, row_count - 1) * _SHEET_GAP
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _sheet_font(36)
    for index, (tick, image) in enumerate(frames):
        column = index % columns
        row = index // columns
        x = _SHEET_MARGIN + column * (cell_w + _SHEET_GAP)
        y = _SHEET_MARGIN + row * (row_h + _SHEET_GAP)
        _draw_sheet_label(draw, f"tick {tick} \u00b7 DTL", x, y, cell_w, font)
        paste_x = x + (cell_w - image.width) // 2
        paste_y = y + _SHEET_LABEL_H + (cell_h - image.height) // 2
        canvas.paste(image, (paste_x, paste_y))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    canvas.save(temporary, format="PNG")
    temporary.replace(path)


def _draw_sheet_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    cell_w: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    text_w = box[2] - box[0]
    text_h = box[3] - box[1]
    draw.text(
        (
            x + (cell_w - text_w) / 2,
            y + (_SHEET_LABEL_H - text_h) / 2 - box[1],
        ),
        text,
        fill=(16, 16, 16),
        font=font,
    )


def _sheet_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        file = Path(r"C:\Windows\Fonts") / name
        if file.is_file():
            return ImageFont.truetype(str(file), size)
    return ImageFont.load_default()


def validate_reply(payload: Mapping[str, Any], sent_ticks: set[int]) -> list[str]:
    """Structural checks only. Catalog V1 evidence rules are not applied.

    A candidate is kept when its id is in the catalog, its confidence is in
    range, and every cited tick was supplied. Preferred view, handedness,
    temporal-state eligibility, baselines, observation counts, exclusive
    pairs, and primary selection do not reject it.
    """

    problems: list[str] = []
    extra = set(payload) - {
        "status",
        "handedness",
        "temporal_orientation",
        "fault_candidates",
    }
    if extra:
        problems.append(f"unexpected fields: {', '.join(sorted(extra))}")
    status = payload.get("status")
    if status not in {"classified", "no_proven_fault"}:
        problems.append("status must be classified or no_proven_fault")
    _handedness(payload, problems)
    _orientation(payload, sent_ticks, problems)
    candidates = payload.get("fault_candidates")
    if not isinstance(candidates, list):
        problems.append("fault_candidates must be a list")
        candidates = []
    if status == "no_proven_fault" and candidates:
        problems.append("no_proven_fault must not name a fault")
    elif status == "classified" and not candidates:
        problems.append("classified reply needs at least one fault candidate")
    for index, candidate in enumerate(candidates):
        _candidate(index, candidate, sent_ticks, problems)
    return problems


def execute(
    shot_id: str,
    *,
    send: bool = False,
    shots_root: Path | None = None,
    results_root: Path | None = None,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """Build the simplified request. OpenAI is called only when send is true."""

    shots = coach.SHOTS if shots_root is None else shots_root
    results = RESULTS if results_root is None else results_root
    _assert_result_namespace(results)
    shot = coach.find_shot(shot_id, shots)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = select_dtl_large(meta, shot)
    moments = plan.moments
    loaded = coach.load_frames(shot, moments)
    request = build_request(moments, loaded)
    breakdown = request_breakdown(
        request, model=coach.MODEL, default_detail=coach.DETAIL
    )
    out = results / shot.name
    out.mkdir(parents=True, exist_ok=True)
    _write(
        out / "request.json", _request_document(shot.name, loaded, request, breakdown)
    )
    write_review_sheet(out / "review_sheet.png", loaded)
    ticks = [moment.capture_tick for moment in moments]
    if not send:
        report = _report(
            shot.name,
            ticks,
            len(loaded),
            breakdown.total_tokens,
            sent=False,
            result_directory=out,
        )
        _write(out / "report.json", report)
        return report

    reply = (
        complete(request)
        if complete is not None
        else asyncio.run(coach.send_openai(request))
    )
    payload = dict(reply.payload)
    problems = validate_reply(payload, set(ticks))
    _write(
        out / "reply.json",
        {
            "model": reply.model,
            "reasoning_effort": coach.REASONING_EFFORT,
            "latency_seconds": reply.latency_seconds,
            "usage": {
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "reasoning_tokens": reply.reasoning_tokens,
            },
            "payload": payload,
        },
    )
    report = _report(
        shot.name,
        ticks,
        len(loaded),
        breakdown.total_tokens,
        sent=True,
        result_directory=out,
        payload=payload,
        problems=problems,
        model=reply.model,
        latency_seconds=reply.latency_seconds,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    _write(out / "report.json", report)
    return report


def format_summary(report: Mapping[str, Any]) -> str:
    ticks = report.get("selected_ticks")
    tick_text = " ".join(str(tick) for tick in ticks) if isinstance(ticks, list) else ""
    fields = report.get("catalog_fields_sent")
    field_text = ", ".join(fields) if isinstance(fields, list) else ""
    lines = [
        f"experiment: {report.get('experiment')}",
        f"shot: {report.get('shot')}",
        f"model: {report.get('model')}",
        f"reasoning_effort: {report.get('reasoning_effort')}",
        f"detail: {report.get('detail')}",
        "camera: DTL",
        "image_budget: LARGE",
        f"temporal_moments: {report.get('temporal_moments')}",
        f"image_count: {report.get('image_count')}",
        f"selected_ticks: {tick_text}",
        f"catalog_fault_count: {report.get('catalog_fault_count')}",
        f"catalog_fields_sent: {field_text}",
        f"estimated_input_tokens: {report.get('estimated_input_tokens')}",
        f"estimated_input_only_cost_usd: {report.get('estimated_cost_usd')}",
        f"sent: {'yes' if report.get('sent') else 'no'}",
        f"result_directory: {report.get('result_directory')}",
    ]
    return "\n".join(lines)


def prompt_text() -> str:
    return f"SYSTEM\n{SYSTEM}\nINSTRUCTIONS\n{INSTRUCTIONS}"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Simplified visual fault classifier. Sends only with --send. "
            "Catalog V1 and the camera-budget matrix are not modified."
        )
    )
    parser.add_argument("shot", help="preserved fresh_v2 shot id")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the request to OpenAI; without this flag nothing is sent",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = execute(args.shot, send=args.send)
    print(format_summary(report))
    print(prompt_text())
    print(f"wrote: {RESULTS / report['shot']}")


def _assert_result_namespace(results_root: Path) -> None:
    resolved = results_root.resolve()
    protected = (coach.RESULTS.resolve(), matrix.RESULTS.resolve())
    for other in protected:
        if resolved == other or _within(resolved, other) or _within(other, resolved):
            raise RuntimeError(
                f"simple visual classifier results cannot overwrite {other}"
            )


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _handedness(payload: Mapping[str, Any], problems: list[str]) -> None:
    raw = payload.get("handedness")
    if not isinstance(raw, dict):
        problems.append("handedness must be an object")
        return
    extra = set(raw) - {"side", "confidence", "visual_basis"}
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"handedness has unexpected fields: {listed}")
    if raw.get("side") not in {"right", "left", "uncertain"}:
        problems.append("handedness.side must be right, left, or uncertain")
    _confidence(raw.get("confidence"), "handedness.confidence", problems)
    basis = raw.get("visual_basis")
    if not isinstance(basis, str) or not basis.strip():
        problems.append("handedness visual_basis is empty")


def _orientation(
    payload: Mapping[str, Any], sent_ticks: set[int], problems: list[str]
) -> None:
    raw = payload.get("temporal_orientation")
    if not isinstance(raw, list):
        problems.append("temporal_orientation must list every sent tick once")
        return
    seen: list[int] = []
    states: dict[int, str] = {}
    for index, item in enumerate(raw):
        where = f"temporal_orientation[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        extra = set(item) - {"capture_tick", "state", "confidence", "visual_basis"}
        if extra:
            listed = ", ".join(sorted(str(field) for field in extra))
            problems.append(f"{where} has unexpected fields: {listed}")
        tick = item.get("capture_tick")
        state = item.get("state")
        if isinstance(tick, bool) or not isinstance(tick, int):
            problems.append(f"{where} capture_tick must be a sent tick")
            continue
        if tick in seen:
            problems.append(f"temporal_orientation capture_tick {tick} is duplicated")
            continue
        seen.append(tick)
        if tick not in sent_ticks:
            problems.append(f"temporal_orientation capture_tick {tick} was not sent")
            continue
        if state not in catalog.TEMPORAL_STATES:
            problems.append(f"{where} state is not allowed")
        elif isinstance(state, str):
            states[tick] = state
        _confidence(item.get("confidence"), f"{where}.confidence", problems)
        basis = item.get("visual_basis")
        if not isinstance(basis, str) or not basis.strip():
            problems.append(f"{where} visual_basis is empty")
    if seen != sorted(seen):
        problems.append("temporal_orientation is not in increasing tick order")
    missing = sorted(sent_ticks - set(seen))
    if missing:
        listed = ", ".join(str(tick) for tick in missing)
        problems.append(f"temporal_orientation is missing sent ticks: {listed}")
    previous: int | None = None
    for tick in sorted(states):
        state = states[tick]
        if state == "uncertain":
            continue
        rank = _STATE_RANK[state]
        if previous is not None and rank < previous:
            problems.append("temporal_orientation regresses chronologically")
            break
        previous = rank


def _candidate(
    index: int, candidate: Any, sent_ticks: set[int], problems: list[str]
) -> None:
    where = f"fault_candidates[{index}]"
    if not isinstance(candidate, dict):
        problems.append(f"{where} must be an object")
        return
    extra = set(candidate) - {"fault_id", "confidence", "evidence"}
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"{where} has unexpected fields: {listed}")
    fault_id = candidate.get("fault_id")
    if not isinstance(fault_id, str) or fault_id not in catalog.FAULTS_BY_ID:
        problems.append(f"{where} fault_id is not in the catalog")
        return
    _confidence(candidate.get("confidence"), f"{where}.confidence", problems)
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        problems.append(f"{where} evidence must cite at least one supplied tick")
        return
    for evidence_index, item in enumerate(evidence):
        _evidence(where, evidence_index, item, sent_ticks, problems)


def _evidence(
    where: str,
    index: int,
    item: Any,
    sent_ticks: set[int],
    problems: list[str],
) -> None:
    label = f"{where}.evidence[{index}]"
    if not isinstance(item, dict):
        problems.append(f"{label} must be an object")
        return
    extra = set(item) - {"capture_tick", "visual_basis"}
    if extra:
        listed = ", ".join(sorted(str(field) for field in extra))
        problems.append(f"{label} has unexpected fields: {listed}")
    tick = item.get("capture_tick")
    if isinstance(tick, bool) or not isinstance(tick, int):
        problems.append(f"{label} capture_tick must be an integer that was sent")
    elif tick not in sent_ticks:
        problems.append(f"{label} capture_tick {tick} was not sent")
    basis = item.get("visual_basis")
    if not isinstance(basis, str) or not basis.strip():
        problems.append(f"{label} visual_basis is empty")


def _confidence(value: Any, label: str, problems: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"{label} must be a number from 0 to 1")
        return
    if not 0 <= float(value) <= 1:
        problems.append(f"{label} must be a number from 0 to 1")


def _report(
    shot: str,
    ticks: Sequence[int],
    image_count: int,
    estimated_tokens: int,
    *,
    sent: bool,
    result_directory: Path,
    payload: Mapping[str, Any] | None = None,
    problems: Sequence[str] | None = None,
    model: str | None = None,
    latency_seconds: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    if sent:
        cost = estimate_cost(model or coach.MODEL, input_tokens, output_tokens)
        cost_basis = "measured_usage"
    else:
        cost = estimate_cost(coach.MODEL, estimated_tokens, 0)
        cost_basis = "estimated_input_tokens_only"
    candidates = None
    status = None
    side = None
    if isinstance(payload, Mapping):
        status = payload.get("status")
        handedness = payload.get("handedness")
        if isinstance(handedness, dict):
            side = handedness.get("side")
        raw_candidates = payload.get("fault_candidates")
        if isinstance(raw_candidates, list):
            candidates = [
                item.get("fault_id")
                for item in raw_candidates
                if isinstance(item, dict) and isinstance(item.get("fault_id"), str)
            ]
    return {
        "shot": shot,
        "experiment": "simple_visual_classifier",
        "arm": "B_simplified_visual_classifier",
        "control": "Catalog V1 DTL_LARGE",
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "detail": coach.DETAIL,
        "camera_mode": CAMERA_MODE,
        "image_budget": IMAGE_BUDGET,
        "selected_ticks": list(ticks),
        "temporal_moments": len(ticks),
        "image_count": image_count,
        "views": ["DTL"],
        "catalog_fault_count": len(catalog.CATALOG),
        "catalog_fields_sent": list(SENT_FIELDS),
        "catalog_fields_withheld": list(WITHHELD_FIELDS),
        "strict_catalog_validator_applied": False,
        "primary_selection_applied": False,
        "coaching_attached": False,
        "estimated_input_tokens": estimated_tokens,
        "estimated_cost_usd": cost,
        "cost_basis": cost_basis,
        "sent": sent,
        "status": status,
        "handedness": side,
        "fault_candidates": candidates,
        "validation_problems": list(problems or []),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "openai_latency_s": (
            None if latency_seconds is None else round(latency_seconds, 3)
        ),
        "result_directory": str(result_directory),
    }


def _request_document(
    shot: str,
    loaded: Sequence[coach.LoadedFrame],
    request: AdviceRequest,
    breakdown: Any,
) -> dict[str, Any]:
    return {
        "shot": shot,
        "experiment": "simple_visual_classifier",
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "detail": coach.DETAIL,
        "max_output_tokens": coach.MAX_OUTPUT_TOKENS,
        "timeout_seconds": coach.TIMEOUT_SECONDS,
        "schema_name": SCHEMA_NAME,
        "camera_mode": CAMERA_MODE,
        "image_budget": IMAGE_BUDGET,
        "catalog_fault_count": len(catalog.CATALOG),
        "catalog_fields_sent": list(SENT_FIELDS),
        "catalog": simplified_catalog(),
        "image_inputs": len(request.images),
        "estimated_input_tokens": breakdown.total_tokens,
        "system": request.system,
        "instructions": request.instructions,
        "evidence": request.evidence,
        "schema": dict(request.schema),
        "images": [
            {
                "view": item.frame.view,
                "capture_tick": item.frame.capture_tick,
                "file": item.frame.file,
                "width": item.width,
                "height": item.height,
                "bytes": len(item.data),
                "sha256": item.sha256,
                "detail": coach.DETAIL,
                "label": image.label,
            }
            for item, image in zip(loaded, request.images, strict=True)
        ],
    }


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
