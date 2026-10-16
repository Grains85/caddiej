"""Camera and image-budget matrix for Catalog V1. Research only.

Nine independent classifier requests. Nothing is sent unless --send is given.
Catalog text, coaching, primary selection, and the default Catalog V1 prompt
are not changed here. Each run sees only the shot and the frozen catalog.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\experiment_runner.py <shot-id>
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\experiment_runner.py <shot-id> --send
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import direct_visual_test as coach
from PIL import Image, ImageDraw, ImageFont

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching.benchmark import request_breakdown
from caddiej.coaching.provider import AdviceRequest

CAMERA_MODES = ("DTL", "FO", "BOTH")
IMAGE_BUDGETS = {"SMALL": 8, "MEDIUM": 16, "LARGE": 24}
MATRIX: tuple[tuple[str, str, str], ...] = tuple(
    (f"{mode}_{budget}", mode, budget)
    for mode in CAMERA_MODES
    for budget in IMAGE_BUDGETS
)
RESULTS = Path(__file__).resolve().parent / "results" / "camera_budget_matrix"

_MARGIN = 28
_GAP = 24
_TICK_COL = 96
_LABEL_H = 44
_BG = (255, 255, 255)
_INK = (16, 16, 16)


def views_for(camera_mode: str) -> tuple[str, ...]:
    if camera_mode == "DTL":
        return ("DTL",)
    if camera_mode == "FO":
        return ("FO",)
    if camera_mode == "BOTH":
        return ("DTL", "FO")
    raise ValueError(f"unknown camera mode: {camera_mode}")


def moment_budget(camera_mode: str, image_budget: str) -> int:
    """Temporal moments that spend the image budget at one or two views."""

    images = IMAGE_BUDGETS[image_budget]
    count = len(views_for(camera_mode))
    if images % count:
        raise ValueError(f"{camera_mode} {image_budget} does not divide evenly")
    return images // count


def run_matrix(
    shot_id: str,
    *,
    send: bool = False,
    shots_root: Path | None = None,
    results_root: Path | None = None,
    run_id: str | None = None,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """Run all nine configurations. Later runs do not see earlier replies."""

    stamp = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    shots = coach.SHOTS if shots_root is None else shots_root
    results = RESULTS if results_root is None else results_root
    shot = coach.find_shot(shot_id, shots)
    root = results / shot.name / stamp
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for configuration, camera_mode, image_budget in MATRIX:
        rows.append(
            run_configuration(
                shot.name,
                camera_mode,
                image_budget,
                configuration=configuration,
                send=send,
                shots_root=shots,
                out_dir=root / configuration,
                complete=complete,
            )
        )
    comparison = build_comparison(shot.name, stamp, rows)
    _write(root / "comparison.json", comparison)
    (root / "comparison.md").write_text(_comparison_md(comparison), encoding="utf-8")
    return comparison


def run_configuration(
    shot_id: str,
    camera_mode: str,
    image_budget: str,
    *,
    configuration: str | None = None,
    send: bool = False,
    shots_root: Path | None = None,
    out_dir: Path,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """One configuration. The request is built only from the shot and the catalog."""

    name = configuration or f"{camera_mode}_{image_budget}"
    views = views_for(camera_mode)
    requested = moment_budget(camera_mode, image_budget)
    shot = coach.find_shot(shot_id, coach.SHOTS if shots_root is None else shots_root)
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    plan = coach.plan_selection(meta, count=requested, shot=shot, views=views)
    moments = plan.moments
    loaded = coach.load_frames(shot, moments)
    request = coach.build_request(moments, loaded)
    breakdown = request_breakdown(
        request, model=coach.MODEL, default_detail=coach.DETAIL
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(
        out_dir / "selected_frames.json",
        _selection_document(shot.name, plan, views, requested),
    )
    _write(
        out_dir / "request.json",
        _request_document(
            shot.name, name, camera_mode, image_budget, loaded, request, breakdown
        ),
    )
    write_review_sheet(out_dir / "review_sheet.png", loaded)
    if not send:
        report = _dry_report(
            shot.name,
            name,
            camera_mode,
            image_budget,
            requested,
            moments,
            loaded,
            breakdown.total_tokens,
        )
        _write(out_dir / "report.json", report)
        return report

    reply = (
        complete(request)
        if complete is not None
        else asyncio.run(coach.send_openai(request))
    )
    payload = dict(reply.payload)
    problems, selection, coaching = coach.interpret(payload, coach.sent_images(moments))
    _write(
        out_dir / "reply.json",
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
    report = _sent_report(
        shot.name,
        name,
        camera_mode,
        image_budget,
        requested,
        moments,
        loaded,
        breakdown.total_tokens,
        payload,
        problems,
        selection,
        coaching,
        reply,
    )
    _write(out_dir / "report.json", report)
    return report


def build_comparison(
    shot: str, run_id: str, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Describe the nine runs. Agreement does not change any of them."""

    ordered = [dict(row) for row in rows]
    accepted = [row for row in ordered if row.get("fault_candidates") is not None]
    no_fault = [
        row["configuration"]
        for row in accepted
        if row.get("status") == "no_proven_fault"
    ]
    proven = [
        row["configuration"]
        for row in accepted
        if row.get("status") == "classified" and row.get("fault_candidates")
    ]
    agreement = _agreement(accepted)
    repeated = [item for item in agreement if item["configurations"] >= 2]
    unique = [item for item in agreement if item["configurations"] == 1]
    return {
        "shot": shot,
        "run_id": run_id,
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "sent": all(bool(row.get("sent")) for row in ordered) if ordered else False,
        "note": (
            "No configuration is marked best. The matrix has no human ground truth. "
            "Agreement is descriptive and is not fed back into any request or into CaddieJ."
        ),
        "rows": ordered,
        "fastest": _fastest(ordered),
        "lowest_input_tokens": _lowest_tokens(ordered),
        "lowest_cost": _lowest_cost(ordered),
        "no_proven_fault": no_fault,
        "proven_faults": proven,
        "faults_repeated": repeated,
        "faults_seen_once": unique,
        "agreement": agreement,
        "agreement_by_camera_mode": _agreement_by(
            accepted, "camera_mode", CAMERA_MODES
        ),
        "agreement_by_image_budget": _agreement_by(
            accepted, "image_budget", tuple(IMAGE_BUDGETS)
        ),
    }


def write_review_sheet(path: Path, loaded: Sequence[coach.LoadedFrame]) -> None:
    """Full-size frames in capture order. Labels are the tick and the view."""

    rows: list[tuple[int, list[coach.LoadedFrame]]] = []
    for item in loaded:
        tick = item.frame.capture_tick
        if rows and rows[-1][0] == tick:
            rows[-1][1].append(item)
        else:
            rows.append((tick, [item]))
    images = [
        (
            tick,
            [
                (frame.frame.view, Image.open(BytesIO(frame.data)).convert("RGB"))
                for frame in frames
            ],
        )
        for tick, frames in rows
    ]
    try:
        _save_sheet(path, images)
    finally:
        for _tick, pair in images:
            for _view, image in pair:
                image.close()


def _save_sheet(
    path: Path, rows: Sequence[tuple[int, list[tuple[str, Image.Image]]]]
) -> None:
    widths = {"DTL": 0, "FO": 0}
    row_h = _LABEL_H
    for _tick, pair in rows:
        tallest = _LABEL_H
        for view, image in pair:
            widths[view] = max(widths[view], image.width)
            tallest = max(tallest, _LABEL_H + image.height)
        row_h = max(row_h, tallest)
    columns = [view for view in ("DTL", "FO") if widths[view]]
    width = _MARGIN + _TICK_COL + _GAP
    for view in columns:
        width += widths[view] + _GAP
    width += _MARGIN - _GAP
    height = _MARGIN + len(rows) * row_h + max(0, len(rows) - 1) * _GAP + _MARGIN
    canvas = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(canvas)
    tick_font = _font(32)
    view_font = _font(26)
    y = _MARGIN
    for tick, pair in rows:
        by_view = {view: image for view, image in pair}
        tick_box = draw.textbbox((0, 0), str(tick), font=tick_font)
        tick_w = tick_box[2] - tick_box[0]
        tick_h = tick_box[3] - tick_box[1]
        draw.text(
            (
                _MARGIN + (_TICK_COL - tick_w) / 2,
                y + (row_h - tick_h) / 2 - tick_box[1],
            ),
            str(tick),
            fill=_INK,
            font=tick_font,
        )
        x = _MARGIN + _TICK_COL + _GAP
        for view in columns:
            label = draw.textbbox((0, 0), view, font=view_font)
            label_w = label[2] - label[0]
            draw.text(
                (
                    x + (widths[view] - label_w) / 2,
                    y + (_LABEL_H - (label[3] - label[1])) / 2 - label[1],
                ),
                view,
                fill=_INK,
                font=view_font,
            )
            image = by_view.get(view)
            if image is not None:
                canvas.paste(image, (x, y + _LABEL_H))
            x += widths[view] + _GAP
        y += row_h + _GAP
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format="PNG")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        file = Path(r"C:\Windows\Fonts") / name
        if file.is_file():
            return ImageFont.truetype(str(file), size)
    return ImageFont.load_default()


def _agreement(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, list[str]] = {}
    for row in rows:
        candidates = row.get("fault_candidates")
        if not isinstance(candidates, list):
            continue
        for fault_id in candidates:
            found.setdefault(str(fault_id), []).append(str(row["configuration"]))
    total = len(rows)
    items = []
    for fault_id in sorted(found):
        configs = found[fault_id]
        items.append(
            {
                "fault_id": fault_id,
                "configurations": len(configs),
                "of": total,
                "where": configs,
            }
        )
    return items


def _agreement_by(
    rows: Sequence[Mapping[str, Any]], key: str, groups: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    return {
        group: _agreement([row for row in rows if row.get(key) == group])
        for group in groups
    }


def _fastest(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    timed = [
        row for row in rows if isinstance(row.get("openai_latency_s"), int | float)
    ]
    if not timed:
        return None
    row = min(
        timed, key=lambda item: (float(item["openai_latency_s"]), item["configuration"])
    )
    return {
        "configuration": row["configuration"],
        "openai_latency_s": row["openai_latency_s"],
    }


def _token_value(row: Mapping[str, Any]) -> int | None:
    measured = row.get("input_tokens")
    if isinstance(measured, int):
        return measured
    estimated = row.get("estimated_input_tokens")
    if isinstance(estimated, int):
        return estimated
    return None


def _lowest_tokens(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    scored = [(row, _token_value(row)) for row in rows]
    scored = [(row, tokens) for row, tokens in scored if tokens is not None]
    if not scored:
        return None
    row, tokens = min(scored, key=lambda item: (item[1], item[0]["configuration"]))
    return {
        "configuration": row["configuration"],
        "input_tokens": tokens,
        "source": "measured"
        if isinstance(row.get("input_tokens"), int)
        else "estimated",
    }


def _lowest_cost(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    scored = [
        row for row in rows if isinstance(row.get("estimated_cost_usd"), int | float)
    ]
    if not scored:
        return None
    row = min(
        scored,
        key=lambda item: (float(item["estimated_cost_usd"]), item["configuration"]),
    )
    return {
        "configuration": row["configuration"],
        "estimated_cost_usd": row["estimated_cost_usd"],
    }


def _dry_report(
    shot: str,
    configuration: str,
    camera_mode: str,
    image_budget: str,
    requested_moments: int,
    moments: Sequence[coach.Moment],
    loaded: Sequence[coach.LoadedFrame],
    estimated_tokens: int,
) -> dict[str, Any]:
    return _base_report(
        shot,
        configuration,
        camera_mode,
        image_budget,
        requested_moments,
        moments,
        loaded,
        estimated_tokens,
        sent=False,
        status=None,
        handedness=None,
        orientation_valid=None,
        fault_candidates=None,
        primary_fault=None,
        primary_confidence=None,
        input_tokens=None,
        output_tokens=None,
        latency=None,
        validation_ok=None,
    )


def _sent_report(
    shot: str,
    configuration: str,
    camera_mode: str,
    image_budget: str,
    requested_moments: int,
    moments: Sequence[coach.Moment],
    loaded: Sequence[coach.LoadedFrame],
    estimated_tokens: int,
    payload: Mapping[str, Any],
    problems: list[str],
    selection: Any,
    coaching: Mapping[str, str] | None,
    reply: Any,
) -> dict[str, Any]:
    ok = not problems
    status: str | None
    candidates: list[str] | None
    if not ok:
        status = "invalid"
        candidates = []
    else:
        status = (
            payload.get("status") if isinstance(payload.get("status"), str) else None
        )
        candidates = _accepted_candidates(payload) if status == "classified" else []
    handedness = payload.get("handedness")
    side = handedness.get("side") if isinstance(handedness, dict) else None
    primary = None if selection is None else selection.fault_id
    confidence = _primary_confidence(selection)
    report = _base_report(
        shot,
        configuration,
        camera_mode,
        image_budget,
        requested_moments,
        moments,
        loaded,
        estimated_tokens,
        sent=True,
        status=status,
        handedness=side,
        orientation_valid=_orientation_valid(problems),
        fault_candidates=candidates,
        primary_fault=primary,
        primary_confidence=confidence,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        latency=None
        if reply.latency_seconds is None
        else round(reply.latency_seconds, 3),
        validation_ok=ok,
    )
    report["caddiej_coaching"] = None if coaching is None else dict(coaching)
    report["validation"] = {"ok": ok, "problems": problems}
    measured = estimate_cost(coach.MODEL, reply.input_tokens, reply.output_tokens)
    report["estimated_cost_usd"] = measured
    report["cost_basis"] = "measured_usage"
    return report


def _base_report(
    shot: str,
    configuration: str,
    camera_mode: str,
    image_budget: str,
    requested_moments: int,
    moments: Sequence[coach.Moment],
    loaded: Sequence[coach.LoadedFrame],
    estimated_tokens: int,
    *,
    sent: bool,
    status: str | None,
    handedness: str | None,
    orientation_valid: bool | None,
    fault_candidates: list[str] | None,
    primary_fault: str | None,
    primary_confidence: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency: float | None,
    validation_ok: bool | None,
) -> dict[str, Any]:
    cost = estimate_cost(coach.MODEL, estimated_tokens, 0)
    return {
        "shot": shot,
        "experiment": "camera_budget_matrix",
        "configuration": configuration,
        "camera_mode": camera_mode,
        "image_budget": image_budget,
        "architecture": "visual_fault_catalog_v1",
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "sent": sent,
        "requested_temporal_moments": requested_moments,
        "temporal_moments": len(moments),
        "image_count": len(loaded),
        "selected_ticks": [moment.capture_tick for moment in moments],
        "status": status,
        "handedness": handedness,
        "orientation_valid": orientation_valid,
        "fault_candidates": fault_candidates,
        "primary_fault": primary_fault,
        "primary_confidence": primary_confidence,
        "estimated_input_tokens": estimated_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "openai_latency_s": latency,
        "estimated_cost_usd": cost,
        "cost_basis": "estimated_input_tokens_only",
        "validation_ok": validation_ok,
        "review_sheet": "review_sheet.png",
    }


def _accepted_candidates(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("fault_candidates")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("fault_id"), str):
            names.append(item["fault_id"])
    return names


def _primary_confidence(selection: Any) -> float | None:
    if selection is None or selection.fault_id is None:
        return None
    for row in selection.ranked:
        if row.fault_id == selection.fault_id and row.disposition == "primary":
            return row.confidence
    return None


def _orientation_valid(problems: Sequence[str]) -> bool:
    return not any("temporal_orientation" in problem for problem in problems)


def _selection_document(
    shot: str,
    plan: coach.Plan,
    views: Sequence[str],
    requested: int,
) -> dict[str, Any]:
    return {
        "shot": shot,
        "views": list(views),
        "requested_temporal_moments": requested,
        "moment_count": len(plan.moments),
        "image_count": sum(len(moment.frames) for moment in plan.moments),
        "max_gap_fraction": coach.max_gap_fraction(requested),
        "selected_capture_ticks": [moment.capture_tick for moment in plan.moments],
        "motion": [score.as_dict() for score in plan.motion],
        "moments": [
            moment.as_dict(i) for i, moment in enumerate(plan.moments, start=1)
        ],
    }


def _request_document(
    shot: str,
    configuration: str,
    camera_mode: str,
    image_budget: str,
    loaded: Sequence[coach.LoadedFrame],
    request: AdviceRequest,
    breakdown: Any,
) -> dict[str, Any]:
    return {
        "shot": shot,
        "configuration": configuration,
        "camera_mode": camera_mode,
        "image_budget": image_budget,
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "architecture": "visual_fault_catalog_v1",
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


def _comparison_md(comparison: Mapping[str, Any]) -> str:
    headers = (
        "configuration",
        "camera_mode",
        "image_budget",
        "temporal_moments",
        "image_count",
        "selected_ticks",
        "status",
        "handedness",
        "orientation_valid",
        "fault_candidates",
        "primary_fault",
        "primary_confidence",
        "input_tokens",
        "output_tokens",
        "openai_latency_s",
        "estimated_cost_usd",
    )
    lines = [
        f"# Camera budget matrix {comparison['shot']} {comparison['run_id']}",
        "",
        comparison["note"],
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in comparison["rows"]:
        lines.append("| " + " | ".join(_cell(row, key) for key in headers) + " |")
    lines.extend(
        [
            "",
            "## Aggregates",
            "",
            f"- fastest: {_compact(comparison['fastest'])}",
            f"- lowest input tokens: {_compact(comparison['lowest_input_tokens'])}",
            f"- lowest cost: {_compact(comparison['lowest_cost'])}",
            f"- no_proven_fault: {_join(comparison['no_proven_fault'])}",
            f"- one or more proven faults: {_join(comparison['proven_faults'])}",
            "",
            "## Agreement",
            "",
        ]
    )
    agreement = comparison["agreement"]
    if not agreement:
        lines.append("No validated replies yet.")
    else:
        total = agreement[0]["of"]
        for item in agreement:
            where = ", ".join(item["where"])
            lines.append(
                f"- {item['fault_id']}: {item['configurations']}/{total} ({where})"
            )
    lines.extend(["", "## Agreement by camera mode", ""])
    lines.extend(_group_lines(comparison["agreement_by_camera_mode"]))
    lines.extend(["", "## Agreement by image budget", ""])
    lines.extend(_group_lines(comparison["agreement_by_image_budget"]))
    lines.append("")
    return "\n".join(lines)


def _cell(row: Mapping[str, Any], key: str) -> str:
    if key == "input_tokens":
        value = row.get("input_tokens")
        if value is None:
            value = row.get("estimated_input_tokens")
            return "-" if value is None else f"{value} est"
    if key == "selected_ticks":
        ticks = row.get("selected_ticks") or []
        return " ".join(str(tick) for tick in ticks)
    if key == "fault_candidates":
        candidates = row.get("fault_candidates")
        if candidates is None:
            return "-"
        return ", ".join(candidates) if candidates else "-"
    value = row.get(key)
    if value is None or value == "":
        return "-"
    return str(value)


def _compact(value: Any) -> str:
    if not isinstance(value, dict):
        return "-"
    return ", ".join(f"{key}={item}" for key, item in value.items())


def _join(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    return ", ".join(str(item) for item in values)


def _group_lines(groups: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for name, items in groups.items():
        if not items:
            lines.append(f"- {name}: -")
            continue
        bits = [
            f"{item['fault_id']} {item['configurations']}/{item['of']}"
            for item in items
        ]
        lines.append(f"- {name}: " + "; ".join(bits))
    return lines


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def format_summary(comparison: Mapping[str, Any]) -> str:
    lines = [
        f"shot: {comparison['shot']}",
        f"run_id: {comparison['run_id']}",
        f"sent: {'yes' if comparison['sent'] else 'no'}",
        f"model: {comparison['model']}  reasoning_effort: {comparison['reasoning_effort']}",
    ]
    for row in comparison["rows"]:
        ticks = " ".join(str(tick) for tick in row["selected_ticks"])
        lines.append(
            f"{row['configuration']}: moments={row['temporal_moments']} "
            f"images={row['image_count']} ticks={ticks} "
            f"estimated_input_tokens={row['estimated_input_tokens']} "
            f"estimated_cost_usd={row['estimated_cost_usd']}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Catalog V1 camera and image-budget matrix. Sends only with --send."
    )
    parser.add_argument("shot", help="preserved fresh_v2 shot id")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send the nine requests to OpenAI; without this flag nothing is sent",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    comparison = run_matrix(args.shot, send=args.send)
    print(format_summary(comparison))
    print(f"wrote: {RESULTS / comparison['shot'] / comparison['run_id']}")


if __name__ == "__main__":
    main()
