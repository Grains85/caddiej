"""Session totals from the shot reports that were actually written.

A missing duration is left out of the sample. It is not counted as zero.
"""

from __future__ import annotations

import math
from typing import Any

from caddiej.diagnostics.store import SESSION_SCHEMA_NAME, SESSION_SCHEMA_VERSION


def build_session_summary(
    session_id: str, reports: list[dict[str, Any]]
) -> dict[str, Any]:
    checkpoints: dict[str, int] = {}
    errors: dict[str, int] = {}
    progression: list[dict[str, Any]] = []
    slowest: list[dict[str, Any]] = []
    assessed = 0
    failed = 0
    superseded = 0
    cancelled = 0
    skipped = 0
    unfinished = 0
    without_feedback = 0
    capture_failures = 0
    for report in reports:
        status = report.get("status")
        if status == "success":
            assessed += 1
        elif status == "failed":
            failed += 1
        elif status == "superseded":
            superseded += 1
        elif status == "cancelled":
            cancelled += 1
        elif status == "skipped":
            skipped += 1
        elif status == "unavailable":
            unfinished += 1
        if _without_feedback(report):
            without_feedback += 1
        capture = report.get("capture") or {}
        if capture.get("status") == "failed":
            capture_failures += 1
        decision = report.get("decision") or {}
        focus = decision.get("selected_checkpoint")
        if focus and decision.get("status") == "success":
            checkpoints[str(focus)] = checkpoints.get(str(focus), 0) + 1
        if decision.get("decision_type"):
            progression.append(
                {
                    "shot_id": (report.get("identity") or {}).get("shot_id"),
                    "decision_type": decision.get("decision_type"),
                    "focus": focus,
                    "progress": decision.get("progress"),
                }
            )
        for message in list(report.get("errors") or ()) + list(
            report.get("warnings") or ()
        ):
            text = str(message)
            if text:
                errors[text] = errors.get(text, 0) + 1
        audio = _measured(report, "shot_to_first_audio")
        if audio is not None:
            slowest.append(
                {
                    "shot_id": (report.get("identity") or {}).get("shot_id"),
                    "span": "shot_to_first_audio",
                    "milliseconds": audio,
                    "quality": "measured",
                }
            )
    slowest.sort(key=lambda item: int(item["milliseconds"]), reverse=True)
    return {
        "schema_name": SESSION_SCHEMA_NAME,
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "shots": len(reports),
        "successfully_assessed": assessed,
        "failed_assessments": failed,
        "superseded": superseded,
        "cancelled": cancelled,
        "skipped": skipped,
        "unfinished": unfinished,
        "without_feedback": without_feedback,
        "capture_failures": capture_failures,
        "checkpoints": [
            {"checkpoint_id": key, "count": checkpoints[key]}
            for key in sorted(checkpoints, key=lambda item: (-checkpoints[item], item))
        ],
        "progression": progression,
        "timing": {
            name: _stats(reports, name)
            for name in (
                "shot_to_capture_completion",
                "evidence_processing",
                "gpt_latency",
                "decision",
                "tts_preparation",
                "shot_to_first_audio",
                "capture_completion_to_first_audio",
                "total_to_completed_feedback",
            )
        },
        "slowest_shots": slowest[:5],
        "recurring_errors": [
            {"message": key, "count": errors[key]}
            for key in sorted(errors, key=lambda item: (-errors[item], item))
        ],
    }


def render_session_markdown(summary: dict[str, Any]) -> str:
    timing = summary.get("timing") or {}
    lines = [
        f"# Session {summary.get('session_id')}",
        "",
        "## Shots",
        "",
        "| | Count |",
        "| --- | --- |",
        f"| Shots | {summary.get('shots', 0)} |",
        f"| Successfully assessed | {summary.get('successfully_assessed', 0)} |",
        f"| Failed assessments | {summary.get('failed_assessments', 0)} |",
        f"| Superseded | {summary.get('superseded', 0)} |",
        f"| Cancelled | {summary.get('cancelled', 0)} |",
        f"| Skipped | {summary.get('skipped', 0)} |",
        f"| Unfinished | {summary.get('unfinished', 0)} |",
        f"| Without confirmed feedback | {summary.get('without_feedback', 0)} |",
        f"| Capture failures | {summary.get('capture_failures', 0)} |",
        "",
    ]
    retained = summary.get("retained_shot_ids") or ()
    if retained:
        lines.append(
            "Counts above include every shot in this session. "
            f"Detailed reports kept: {len(retained)}."
        )
        lines.append("")
    lines.extend(["## Checkpoints", ""])
    checkpoints = summary.get("checkpoints") or ()
    if not checkpoints:
        lines.append("No checkpoint was selected.")
    else:
        lines.extend(
            [
                "| Checkpoint | Shots |",
                "| --- | --- |",
            ]
        )
        for item in checkpoints:
            lines.append(f"| {item.get('checkpoint_id')} | {item.get('count')} |")
    lines.extend(["", "## Progression", ""])
    progression = summary.get("progression") or ()
    if not progression:
        lines.append("No coaching decision was recorded.")
    else:
        lines.extend(
            [
                "| Shot | Decision | Focus | Progress |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in progression:
            lines.append(
                f"| {item.get('shot_id') or '—'} | {item.get('decision_type') or '—'} | "
                f"{item.get('focus') or '—'} | {item.get('progress') or '—'} |"
            )
    lines.extend(
        ["", "## Timing", "", "Medians and p95 use measured durations only.", ""]
    )
    lines.extend(
        [
            "| Span | Sample | Median | p95 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for name, stats in timing.items():
        lines.append(
            f"| {name} | {stats.get('sample_size', 0)} | "
            f"{_ms(stats.get('median_ms'))} | {_ms(stats.get('p95_ms'))} |"
        )
    lines.extend(["", "## Slowest shots", ""])
    slowest = summary.get("slowest_shots") or ()
    if not slowest:
        lines.append("No measured shot-to-audio durations.")
    else:
        lines.extend(["| Shot | Span | Duration |", "| --- | --- | --- |"])
        for item in slowest:
            lines.append(
                f"| {item.get('shot_id')} | {item.get('span')} | "
                f"{item.get('milliseconds')} ms |"
            )
    lines.extend(["", "## Recurring errors", ""])
    recurring = summary.get("recurring_errors") or ()
    if not recurring:
        lines.append("None.")
    else:
        for item in recurring:
            lines.append(f"- {item.get('count')}× {item.get('message')}")
    lines.append("")
    return "\n".join(lines)


def _without_feedback(report: dict[str, Any]) -> bool:
    """No confirmed floor line and no confirmed playback.

    A coaching sentence that was only queued does not count as feedback.
    A shot with no sentence at all does not count either.
    """

    delivery = report.get("delivery") or {}
    hud = (delivery.get("hud") or {}).get("status")
    playback = (delivery.get("tts_playback") or {}).get("status")
    return hud != "success" and playback != "success"


def _measured(report: dict[str, Any], name: str) -> int | None:
    for span in (report.get("timing") or {}).get("spans") or ():
        if span.get("name") == name and span.get("quality") == "measured":
            milliseconds = span.get("milliseconds")
            if isinstance(milliseconds, int):
                return milliseconds
    return None


def _stats(reports: list[dict[str, Any]], name: str) -> dict[str, Any]:
    values = [
        value for report in reports if (value := _measured(report, name)) is not None
    ]
    return {
        "sample_size": len(values),
        "median_ms": _median(values),
        "p95_ms": _percentile(values, 0.95),
        "quality": "measured",
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return int(round((ordered[middle - 1] + ordered[middle]) / 2))


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(fraction * len(ordered)) - 1
    index = min(max(index, 0), len(ordered) - 1)
    return ordered[index]


def _ms(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value} ms"
