"""Frozen sampler frames into the frozen simplified visual classifier.

Research only. temporal_sampler.py is not imported and is not modified.
The simplified catalog, schema, model, reasoning effort, and image detail
come from simple_visual_classifier.py. Nothing is sent unless --send is given.

    .venv\\Scripts\\python.exe research\\direct_visual_coach\\temporal_sampler_classifier.py <shot-id> [<shot-id> ...]
    .venv\\Scripts\\python.exe research\\direct_visual_coach\\temporal_sampler_classifier.py <shot-id> --send
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import direct_visual_test as coach
import simple_visual_classifier as simple

from caddiej.adapters.openai.pricing import estimate_cost
from caddiej.coaching.benchmark import request_breakdown
from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.video.jpeg_size import jpeg_size

EXPECTED_FRAMES = 24
SAMPLER_RESULTS = Path(__file__).resolve().parent / "results" / "temporal_sampler"
RESULTS = Path(__file__).resolve().parent / "results" / "temporal_sampler_classifier"
_P_PHASE = re.compile(r"\bP[1-9]\b", re.IGNORECASE)
_PHASE_ON_FRAME = re.compile(
    r"\b(?:impact|transition|downswing|follow-through|follow_through|"
    r"backswing|setup|top)\b",
    re.IGNORECASE,
)
_MOTION_LEAK = (
    "motion score",
    "motion_score",
    "combined_motion",
    "optical flow",
    "optical_flow",
    "optical-flow",
    "high-motion",
    "high_motion",
    "farneback",
)
_SELECTION_LEAK = (
    "selection_reason",
    "sequence_start",
    "gap_fill",
    "selected_sheet",
    "contact anchor",
    "contact_anchor",
)


@dataclass(frozen=True, slots=True)
class SelectedSource:
    """One original frame named by the frozen sampler manifest."""

    order: int
    capture_tick: int
    relative_timestamp_seconds: float
    path: Path
    width: int
    height: int
    recorded_sha256: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def load_selected_frames(
    shot_id: str, sampler_root: Path | None = None
) -> list[SelectedSource]:
    """Read selected_frames.json and the original JPEGs it names."""

    root = SAMPLER_RESULTS if sampler_root is None else sampler_root
    document_path = root / shot_id / "selected_frames.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    if document.get("shot_id") not in {None, shot_id}:
        raise ValueError(f"{document_path} is for {document.get('shot_id')}")
    rows = document.get("frames")
    if not isinstance(rows, list):
        raise ValueError(f"{document_path} has no frames")
    loaded = [_source(row, index) for index, row in enumerate(rows)]
    _require_exact_selection(loaded)
    return loaded


def build_request(frames: Sequence[SelectedSource]) -> AdviceRequest:
    """The simplified classifier request, with these frames as the only images."""

    _require_exact_selection(frames)
    total = len(frames)
    images = []
    for ordinal, frame in enumerate(frames, start=1):
        images.append(
            ImageInput(
                data=frame.data,
                label=(
                    f"DTL capture_tick {frame.capture_tick}, "
                    f"relative_timestamp_seconds {frame.relative_timestamp_seconds:.4f}, "
                    f"Down-the-Line, image {ordinal} of {total}"
                ),
                detail=coach.DETAIL,
            )
        )
    return AdviceRequest(
        system=simple.SYSTEM,
        instructions=simple.INSTRUCTIONS,
        evidence=evidence_text(frames),
        schema=simple.schema(),
        images=tuple(images),
        max_output_tokens=coach.MAX_OUTPUT_TOKENS,
        timeout_seconds=coach.TIMEOUT_SECONDS,
        schema_name=simple.SCHEMA_NAME,
        reasoning_effort=coach.REASONING_EFFORT,
    )


def evidence_text(frames: Sequence[SelectedSource]) -> str:
    """Chronological tick and time identity. No reason for the selection."""

    total = len(frames)
    lines = [
        "No launch-monitor image and no launch-monitor numbers are attached.",
        "",
        (
            f"The images are {total} separate original full-resolution "
            "Down-the-Line frames. No Face-On frame is attached."
        ),
        "They are chronological.",
        (
            "Each image has capture_tick. "
            "A larger capture_tick is later. A smaller capture_tick is earlier."
        ),
        (
            "Each image has relative_timestamp_seconds, "
            "seconds from the first captured frame of this recording."
        ),
        (
            "Frame spacing is intentionally non-uniform. "
            "A smaller time gap means the local sampler chose denser temporal coverage."
        ),
        "DTL is Swing Behind (down the line). Do not assume a Face-On view exists.",
        "temporal_orientation must contain each of these capture ticks once.",
        "",
    ]
    previous: float | None = None
    for ordinal, frame in enumerate(frames, start=1):
        line = (
            f"Image {ordinal} of {total}: capture_tick {frame.capture_tick}, "
            f"relative_timestamp_seconds {frame.relative_timestamp_seconds:.4f}"
        )
        if previous is not None:
            gap = frame.relative_timestamp_seconds - previous
            line += f", seconds_since_previous_image {gap:.4f}"
        lines.append(line)
        previous = frame.relative_timestamp_seconds
    lines.extend(
        [
            "",
            "VISUAL FAULT CATALOG",
            "Each object is one existing catalog fault. "
            "The fields are id, name, visual_definition, and preferred_views. "
            "preferred_views names the camera that usually shows the fault. "
            "Only Down-the-Line frames are attached. "
            "Judge each visual_definition from those frames.",
            simple.simplified_catalog_json(),
        ]
    )
    return "\n".join(lines)


def request_audit(
    request: AdviceRequest, frames: Sequence[SelectedSource]
) -> dict[str, bool]:
    """True where a forbidden input is present. All values should be false."""

    frame_text = _frame_text(request)
    whole = "\n".join(
        (
            request.system,
            request.instructions,
            request.evidence,
            "\n".join(image.label for image in request.images),
        )
    )
    lowered = whole.lower()
    paths = [frame.path.name.lower() for frame in frames]
    return {
        "contact_anchor": ("contact anchor" in lowered or "contact_anchor" in lowered),
        "p1_p9": _P_PHASE.search(whole) is not None,
        "motion_scores": any(phrase in lowered for phrase in _MOTION_LEAK),
        "optical_flow": any(
            phrase in lowered
            for phrase in ("optical flow", "optical_flow", "optical-flow")
        ),
        "phase_labels_supplied": _PHASE_ON_FRAME.search(frame_text) is not None,
        "selected_sheet_sent": any("selected_sheet" in name for name in paths)
        or "selected_sheet" in lowered,
        "selection_reason_sent": any(phrase in lowered for phrase in _SELECTION_LEAK),
    }


def execute(
    shot_id: str,
    *,
    send: bool = False,
    sampler_root: Path | None = None,
    results_root: Path | None = None,
    complete: Callable[[AdviceRequest], Any] | None = None,
) -> dict[str, Any]:
    """Build one independent request. OpenAI is called only when send is true."""

    results = RESULTS if results_root is None else results_root
    _assert_result_namespace(results)
    frames = load_selected_frames(shot_id, sampler_root)
    request = build_request(frames)
    problems = request_audit(request, frames)
    if any(problems.values()):
        listed = ", ".join(name for name, present in problems.items() if present)
        raise RuntimeError(f"{shot_id} request failed the input audit: {listed}")
    breakdown = request_breakdown(
        request, model=coach.MODEL, default_detail=coach.DETAIL
    )
    out = results / shot_id
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "request.json", _request_document(shot_id, frames, request, breakdown))
    ticks = [frame.capture_tick for frame in frames]
    if not send:
        report = _report(
            shot_id,
            frames,
            breakdown.total_tokens,
            sum(len(frame.data) for frame in frames),
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
    validation = simple.validate_reply(payload, set(ticks))
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
        shot_id,
        frames,
        breakdown.total_tokens,
        sum(len(frame.data) for frame in frames),
        sent=True,
        result_directory=out,
        payload=payload,
        problems=validation,
        model=reply.model,
        latency_seconds=reply.latency_seconds,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    _write(out / "report.json", report)
    return report


def format_summary(report: Mapping[str, Any]) -> str:
    ticks = report.get("selected_ticks")
    times = report.get("relative_timestamps")
    size = report.get("image_dimensions")
    tick_text = " ".join(str(tick) for tick in ticks) if isinstance(ticks, list) else ""
    time_text = (
        " ".join(str(value) for value in times) if isinstance(times, list) else ""
    )
    size_text = " ".join(str(value) for value in size) if isinstance(size, list) else ""
    lines = [
        f"shot: {report.get('shot')}",
        f"image_count: {report.get('image_count')}",
        f"selected_ticks: {tick_text}",
        f"relative_timestamps: {time_text}",
        f"image_dimensions: {size_text}",
        f"model: {report.get('model')}",
        f"reasoning_effort: {report.get('reasoning_effort')}",
        f"detail: {report.get('detail')}",
        f"catalog_fault_count: {report.get('catalog_fault_count')}",
        f"estimated_input_tokens: {report.get('estimated_input_tokens')}",
        f"image_bytes: {report.get('image_bytes')}",
        f"estimated_input_only_cost_usd: {report.get('estimated_cost_usd')}",
        "contact_anchor_in_request: no",
        "p1_p9_in_request: no",
        "motion_scores_in_request: no",
        "optical_flow_in_request: no",
        "phase_labels_supplied: no",
        "selected_sheet_sent: no",
        "original_source_images_sent: yes",
        f"sent: {'yes' if report.get('sent') else 'no'}",
        f"result_directory: {report.get('result_directory')}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Simplified visual classifier on frozen temporal-sampler frames. "
            "Sends only with --send. One shot is one independent request."
        )
    )
    parser.add_argument("shots", nargs="+", help="archived shot id")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send each request to OpenAI; without this flag nothing is sent",
    )
    parser.add_argument("--sampler-root", type=Path, default=None)
    parser.add_argument("--results-root", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    for shot_id in args.shots:
        report = execute(
            shot_id,
            send=args.send,
            sampler_root=args.sampler_root,
            results_root=args.results_root,
        )
        print(format_summary(report))
        print()


def _source(row: Any, index: int) -> SelectedSource:
    if not isinstance(row, dict):
        raise ValueError(f"frame {index} is not an object")
    path = Path(str(row.get("path") or ""))
    if not path.is_file():
        raise ValueError(f"original frame is missing: {path}")
    if path.name.lower() == "selected_sheet.jpg":
        raise ValueError("selected_sheet.jpg cannot be a source frame")
    data = path.read_bytes()
    measured = jpeg_size(data)
    width = row.get("width")
    height = row.get("height")
    if measured is None or measured != (width, height):
        raise ValueError(f"{path} dimensions do not match the manifest")
    recorded = str(row.get("sha256") or "")
    digest = hashlib.sha256(data).hexdigest()
    if not recorded or not digest.startswith(recorded):
        raise ValueError(f"{path} does not match the recorded sha256")
    tick = row.get("capture_tick")
    elapsed = row.get("elapsed_seconds")
    order = row.get("order")
    if isinstance(tick, bool) or not isinstance(tick, int):
        raise ValueError(f"{path} capture_tick is missing")
    if isinstance(elapsed, bool) or not isinstance(elapsed, int | float):
        raise ValueError(f"{path} elapsed_seconds is missing")
    if isinstance(order, bool) or not isinstance(order, int):
        raise ValueError(f"{path} order is missing")
    return SelectedSource(
        order=order,
        capture_tick=tick,
        relative_timestamp_seconds=float(elapsed),
        path=path,
        width=int(width),
        height=int(height),
        recorded_sha256=recorded,
        data=data,
    )


def _require_exact_selection(frames: Sequence[SelectedSource]) -> None:
    if len(frames) != EXPECTED_FRAMES:
        raise ValueError(f"expected {EXPECTED_FRAMES} frames, found {len(frames)}")
    orders = [frame.order for frame in frames]
    ticks = [frame.capture_tick for frame in frames]
    times = [frame.relative_timestamp_seconds for frame in frames]
    paths = [frame.path.resolve() for frame in frames]
    digests = [frame.sha256 for frame in frames]
    if orders != list(range(EXPECTED_FRAMES)):
        raise ValueError("frame order is not 0..23")
    if ticks != sorted(ticks) or len(set(ticks)) != EXPECTED_FRAMES:
        raise ValueError("capture ticks are not unique and chronological")
    if times != sorted(times) or len(set(times)) != EXPECTED_FRAMES:
        raise ValueError("relative timestamps are not unique and chronological")
    if len(set(paths)) != EXPECTED_FRAMES or len(set(digests)) != EXPECTED_FRAMES:
        raise ValueError("source frames are not unique")
    for earlier, later in zip(times, times[1:], strict=False):
        if later <= earlier:
            raise ValueError("relative timestamps do not move forward")


def _frame_text(request: AdviceRequest) -> str:
    evidence = request.evidence
    marker = "\nVISUAL FAULT CATALOG\n"
    head = evidence.split(marker, maxsplit=1)[0]
    labels = "\n".join(image.label for image in request.images)
    return f"{head}\n{labels}"


def _assert_result_namespace(results_root: Path) -> None:
    resolved = results_root.resolve()
    protected = (
        SAMPLER_RESULTS.resolve(),
        simple.RESULTS.resolve(),
        coach.RESULTS.resolve(),
    )
    for other in protected:
        if resolved == other or _within(resolved, other) or _within(other, resolved):
            raise RuntimeError(
                f"temporal sampler classifier results cannot overwrite {other}"
            )


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _report(
    shot: str,
    frames: Sequence[SelectedSource],
    estimated_tokens: int,
    image_bytes: int,
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
    if isinstance(payload, Mapping):
        status = payload.get("status")
        raw_candidates = payload.get("fault_candidates")
        if isinstance(raw_candidates, list):
            candidates = [
                item.get("fault_id")
                for item in raw_candidates
                if isinstance(item, dict) and isinstance(item.get("fault_id"), str)
            ]
    return {
        "shot": shot,
        "experiment": "temporal_sampler_classifier",
        "classifier": "simple_visual_classifier",
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "detail": coach.DETAIL,
        "selected_ticks": [frame.capture_tick for frame in frames],
        "relative_timestamps": [frame.relative_timestamp_seconds for frame in frames],
        "image_dimensions": [f"{frame.width}x{frame.height}" for frame in frames],
        "image_count": len(frames),
        "image_bytes": image_bytes,
        "catalog_fault_count": len(simple.simplified_catalog()),
        "catalog_fields_sent": list(simple.SENT_FIELDS),
        "strict_catalog_validator_applied": False,
        "primary_selection_applied": False,
        "coaching_attached": False,
        "contact_anchor_supplied": False,
        "estimated_input_tokens": estimated_tokens,
        "estimated_cost_usd": cost,
        "cost_basis": cost_basis,
        "sent": sent,
        "status": status,
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
    frames: Sequence[SelectedSource],
    request: AdviceRequest,
    breakdown: Any,
) -> dict[str, Any]:
    return {
        "shot": shot,
        "experiment": "temporal_sampler_classifier",
        "classifier": "simple_visual_classifier",
        "model": coach.MODEL,
        "reasoning_effort": coach.REASONING_EFFORT,
        "detail": coach.DETAIL,
        "max_output_tokens": coach.MAX_OUTPUT_TOKENS,
        "timeout_seconds": coach.TIMEOUT_SECONDS,
        "schema_name": simple.SCHEMA_NAME,
        "catalog_fault_count": len(simple.simplified_catalog()),
        "catalog_fields_sent": list(simple.SENT_FIELDS),
        "catalog": simple.simplified_catalog(),
        "image_inputs": len(request.images),
        "estimated_input_tokens": breakdown.total_tokens,
        "image_bytes": sum(len(frame.data) for frame in frames),
        "system": request.system,
        "instructions": request.instructions,
        "evidence": request.evidence,
        "schema": dict(request.schema),
        "images": [
            {
                "capture_tick": frame.capture_tick,
                "relative_timestamp_seconds": frame.relative_timestamp_seconds,
                "path": str(frame.path),
                "width": frame.width,
                "height": frame.height,
                "bytes": len(frame.data),
                "sha256": frame.sha256,
                "detail": image.detail,
                "label": image.label,
            }
            for frame, image in zip(frames, request.images, strict=True)
        ],
    }


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
