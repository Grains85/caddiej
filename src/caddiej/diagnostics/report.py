"""One shot report, as JSON and as a page a person can read.

Missing facts stay missing. A duration is measured only when both of its
monotonic marks were recorded. Playback is confirmed only from a playback
event, never from a request to speak.
"""

from __future__ import annotations

from typing import Any

from caddiej.diagnostics.store import SHOT_SCHEMA_NAME, SHOT_SCHEMA_VERSION, scrub

_SPANS = (
    (
        "shot_to_capture_completion",
        "Shot to capture completion",
        "shot_completed",
        "swing_frames_ready",
        False,
    ),
    (
        "evidence_processing",
        "Evidence processing",
        "evidence_started",
        "evidence_completed",
        False,
    ),
    ("gpt_latency", "GPT latency", "vision_started", "vision_received", True),
    (
        "decision",
        "Coaching decision",
        "assessment_validated",
        "coaching_decision_completed",
        False,
    ),
    (
        "tts_preparation",
        "TTS preparation",
        "tts_requested",
        "tts_started",
        False,
    ),
    (
        "shot_to_first_audio",
        "Shot to first audio",
        "shot_detected",
        "tts_started",
        False,
    ),
    (
        "simulator_shot_to_first_audio",
        "Simulator result to first audio",
        "shot_completed",
        "tts_started",
        False,
    ),
    (
        "capture_completion_to_first_audio",
        "Capture completion to first audio",
        "swing_frames_ready",
        "tts_started",
        False,
    ),
    (
        "total_to_completed_feedback",
        "Total time to completed feedback",
        "shot_detected",
        "tts_completed",
        False,
    ),
    (
        "hud_delivery",
        "HUD delivered",
        "shot_completed",
        "hud_shown",
        False,
    ),
)


def build_shot_report(facts: dict[str, Any]) -> dict[str, Any]:
    """The machine-readable report. `facts` is not modified."""

    capture = _section(facts.get("capture"), "Capture was not recorded")
    evidence = _section(facts.get("evidence"), "Evidence was not recorded")
    gpt = _gpt(facts.get("gpt"))
    decision = _section(facts.get("decision"), "No coaching decision was recorded")
    hud = _hud(facts)
    playback = _playback(facts)
    request = _section(facts.get("tts_request"), "No speech request was recorded")
    timing = _timing(facts)
    errors = [str(item) for item in facts.get("errors") or ()]
    warnings = [str(item) for item in facts.get("warnings") or ()]
    status = _overall(facts, capture, evidence, gpt, decision)
    report = {
        "schema_name": SHOT_SCHEMA_NAME,
        "schema_version": SHOT_SCHEMA_VERSION,
        "status": status,
        "status_reason": _reason(facts, status),
        "identity": {
            "shot_id": facts.get("shot_id"),
            "sequence": facts.get("sequence"),
            "session_id": facts.get("session_id"),
            "capture_id": facts.get("capture_id"),
            "trigger_id": facts.get("trigger_id"),
            "strike_id": facts.get("strike_id"),
            "simulator_shot_id": facts.get("simulator_shot_id"),
        },
        "relationships": list(facts.get("relationships") or ()),
        "milestones": _milestones(facts.get("milestones") or {}),
        "club": facts.get("club"),
        "measurements": facts.get("measurements"),
        "capture": capture,
        "evidence": evidence,
        "gpt": gpt,
        "decision": decision,
        "delivery": {
            "hud": hud,
            "tts_request": request,
            "tts_playback": playback,
        },
        "timing": timing,
        "contact_sheet": facts.get("contact_sheet")
        or {
            "status": "unavailable",
            "file": None,
            "detail": "contact sheet was not generated",
        },
        "errors": errors,
        "warnings": warnings,
    }
    return scrub(report)


def render_shot_markdown(report: dict[str, Any]) -> str:
    """The same report, readable without the codebase."""

    identity = report.get("identity") or {}
    lines = [
        f"# Shot {identity.get('shot_id') or 'unknown'}",
        "",
        "## 1. Overall status",
        "",
        f"**{report.get('status', 'unavailable')}.** {report.get('status_reason') or ''}",
        "",
        _identity_table(identity, report.get("relationships") or ()),
        "",
        "## 2. Capture",
        "",
        _capture_section(report.get("capture") or {}),
        "",
        "## 3. Evidence",
        "",
        _evidence_section(report.get("evidence") or {}),
        "",
        "## 4. GPT assessment",
        "",
        _gpt_section(report.get("gpt") or {}),
        "",
        "## 5. Coaching decision",
        "",
        _decision_section(report.get("decision") or {}),
        "",
        "## 6. Feedback delivery",
        "",
        _delivery_section(report.get("delivery") or {}),
        "",
        "## 7. Performance timeline",
        "",
        _timing_section(report.get("timing") or {}),
        "",
        "## 8. Warnings and errors",
        "",
        _bullet("Warnings", report.get("warnings") or ()),
        "",
        _bullet("Errors", report.get("errors") or ()),
        "",
    ]
    return "\n".join(lines)


def _overall(
    facts: dict[str, Any],
    capture: dict[str, Any],
    evidence: dict[str, Any],
    gpt: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    terminal = facts.get("terminal") or {}
    status = terminal.get("status")
    if status in {"superseded", "cancelled", "skipped"}:
        return str(status)
    if status == "failed":
        return "failed"
    if status == "success":
        return "success"
    if any(
        step.get("status") == "failed" for step in (capture, evidence, gpt, decision)
    ):
        return "failed"
    if capture.get("status") == "cancelled":
        return "cancelled"
    if decision.get("status") == "success":
        return "success"
    return "unavailable"


def _reason(facts: dict[str, Any], status: str) -> str:
    terminal = facts.get("terminal") or {}
    if terminal.get("reason"):
        return str(terminal["reason"])
    if status == "unavailable":
        return "this shot has no completed coaching outcome"
    return ""


def _section(value: Any, missing: str) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("status"):
        return dict(value)
    return {"status": "unavailable", "detail": missing}


def _gpt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("status"):
        return {
            "status": "unavailable",
            "detail": "no model request was recorded",
            "validation_status": "unavailable",
            "validation_errors": [],
            "accepted_findings": [],
            "rejected_findings": [],
            "parsed_assessment": None,
        }
    gpt = dict(value)
    gpt.setdefault("validation_status", "unavailable")
    gpt.setdefault("validation_errors", [])
    gpt.setdefault("accepted_findings", [])
    gpt.setdefault("rejected_findings", [])
    gpt.setdefault("parsed_assessment", None)
    return gpt


def _hud(facts: dict[str, Any]) -> dict[str, Any]:
    recorded = facts.get("hud")
    if isinstance(recorded, dict) and recorded.get("status"):
        return dict(recorded)
    decision = facts.get("decision") or {}
    text = decision.get("hud_text") or ""
    terminal = (facts.get("terminal") or {}).get("status")
    if terminal == "superseded":
        return {
            "status": "superseded",
            "text": text,
            "detail": "the shot was superseded before the floor confirmed the line",
        }
    if terminal == "cancelled":
        return {
            "status": "cancelled",
            "text": text,
            "detail": "the shot was cancelled before the floor confirmed the line",
        }
    if not text:
        return {"status": "skipped", "text": "", "detail": "no floor text was produced"}
    return {
        "status": "unavailable",
        "text": text,
        "detail": "floor display was not confirmed",
    }


def _playback(facts: dict[str, Any]) -> dict[str, Any]:
    """Spoken audio only when a playback-start milestone was observed."""

    started = _marked(facts, "tts_started")
    recorded = facts.get("tts_playback")
    if isinstance(recorded, dict) and recorded.get("status"):
        playback = dict(recorded)
        if playback.get("status") == "success" and not started:
            playback["status"] = "unavailable"
            playback["detail"] = (
                "a completion claimed spoken audio without a playback start event"
            )
            playback["spoken_seconds"] = None
        return playback
    request = facts.get("tts_request") or {}
    if request.get("status") == "success" and not started:
        return {
            "status": "unavailable",
            "outcome": None,
            "spoken_seconds": None,
            "detail": "speech was requested; playback did not start",
        }
    if request.get("status") in {"failed", "skipped", "cancelled", "superseded"}:
        return {
            "status": "skipped",
            "outcome": None,
            "spoken_seconds": None,
            "detail": "speech was not played",
        }
    return {
        "status": "unavailable",
        "outcome": None,
        "spoken_seconds": None,
        "detail": "playback was not observed",
    }


def _marked(facts: dict[str, Any], name: str) -> bool:
    mark = (facts.get("milestones") or {}).get(name) or {}
    return mark.get("monotonic_seconds") is not None or mark.get("observed") is True


def _milestones(marks: dict[str, Any]) -> list[dict[str, Any]]:
    observed: list[dict[str, Any]] = []
    for name, mark in marks.items():
        if not isinstance(mark, dict):
            continue
        if mark.get("monotonic_seconds") is None and not mark.get("utc"):
            continue
        observed.append(
            {
                "name": name,
                "utc": mark.get("utc"),
                "monotonic_seconds": mark.get("monotonic_seconds"),
                "quality": mark.get("quality") or "measured",
            }
        )
    return observed


def _timing(facts: dict[str, Any]) -> dict[str, Any]:
    simulated = bool(facts.get("simulated_provider"))
    spans = []
    for key, label, start, end, provider in _SPANS:
        spans.append(
            _span(
                facts,
                key=key,
                label=label,
                start=start,
                end=end,
                estimated=provider and simulated,
            )
        )
    pieces = []
    for name, milliseconds in sorted((facts.get("evidence_timing_ms") or {}).items()):
        if milliseconds is None:
            pieces.append(
                {"name": name, "milliseconds": None, "quality": "unavailable"}
            )
        else:
            pieces.append(
                {
                    "name": name,
                    "milliseconds": int(milliseconds),
                    "quality": "measured",
                }
            )
    return {"spans": spans, "evidence_stages": pieces, "simulated_provider": simulated}


def _span(
    facts: dict[str, Any],
    *,
    key: str,
    label: str,
    start: str,
    end: str,
    estimated: bool,
) -> dict[str, Any]:
    marks = facts.get("milestones") or {}
    left = marks.get(start) or {}
    right = marks.get(end) or {}
    start_at = left.get("monotonic_seconds")
    end_at = right.get("monotonic_seconds")
    if start_at is None or end_at is None:
        return {
            "name": key,
            "label": label,
            "milliseconds": None,
            "quality": "unavailable",
            "start": start,
            "end": end,
        }
    milliseconds = int(round((float(end_at) - float(start_at)) * 1000))
    if milliseconds < 0:
        return {
            "name": key,
            "label": label,
            "milliseconds": None,
            "quality": "unavailable",
            "start": start,
            "end": end,
            "detail": "the end mark preceded the start mark",
        }
    quality = "estimated" if estimated else "measured"
    span = {
        "name": key,
        "label": label,
        "milliseconds": milliseconds,
        "quality": quality,
        "start": start,
        "end": end,
    }
    if estimated:
        span["detail"] = "simulated provider delay, not measured GPT latency"
    return span


def _identity_table(identity: dict[str, Any], relationships: list[Any]) -> str:
    rows = ["| Field | Value |", "| --- | --- |"]
    for key in (
        "shot_id",
        "sequence",
        "session_id",
        "capture_id",
        "trigger_id",
        "strike_id",
        "simulator_shot_id",
    ):
        rows.append(f"| {key} | {_cell(identity.get(key))} |")
    lines = ["| Identity | |", *rows[1:]]
    if relationships:
        lines.extend(["", "Relationships:", ""])
        for item in relationships:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('name')}: {_cell(item.get('value'))} "
                f"({item.get('source') or 'recorded'})"
            )
    return "\n".join(lines)


def _capture_section(capture: dict[str, Any]) -> str:
    lines = [
        f"Status: **{capture.get('status', 'unavailable')}.** "
        f"{capture.get('detail') or ''}",
        "",
        "| Camera | Source | Status | Captured | Kept | Failed | Detail |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    cameras = capture.get("cameras") or ()
    if not cameras:
        lines.append("| — | — | unavailable | — | — | — | no cameras recorded |")
    for camera in cameras:
        ok = camera.get("ok")
        state = "success" if ok else "failed"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(camera.get("camera_id")),
                    _cell(camera.get("source")),
                    state,
                    _cell(camera.get("captured")),
                    _cell(camera.get("kept")),
                    _cell(camera.get("failed")),
                    _cell(camera.get("detail")),
                )
            )
            + " |"
        )
    started = capture.get("started_at")
    detected = capture.get("detected_at")
    lines.extend(
        [
            "",
            f"Capture started (wall clock): {_cell(started)}",
            f"Strike detected (wall clock): {_cell(detected)}",
            f"Directory: {_cell(capture.get('directory'))}",
        ]
    )
    return "\n".join(lines)


def _evidence_section(evidence: dict[str, Any]) -> str:
    lines = [
        f"Status: **{evidence.get('status', 'unavailable')}.** "
        f"{evidence.get('detail') or ''}",
        "",
        "| Camera | Tick | Time (s) | Stage | To the model | Reason | File |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    frames = evidence.get("selected_frames") or ()
    if not frames:
        lines.append("| — | — | — | — | — | — | no selected frames recorded |")
    for frame in frames:
        stage = frame.get("stage") or "stage unknown"
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(frame.get("camera_id")),
                    _cell(frame.get("capture_tick")),
                    _fmt_seconds(frame.get("timestamp_seconds")),
                    _cell(stage),
                    _transmission(frame),
                    _cell(frame.get("selection_reason")),
                    _cell(frame.get("relative_file")),
                )
            )
            + " |"
        )
    request_only = evidence.get("request_only_images") or ()
    if request_only:
        lines.extend(["", "In the model request, and not in the selected set:", ""])
        for frame in request_only:
            lines.append(
                f"- {_cell(frame.get('relative_file'))}: {_transmission(frame)}"
            )
    lines.extend(["", "### Stages", ""])
    stages = evidence.get("stages") or ()
    if not stages:
        lines.append("No stage detection was recorded.")
    for stage in stages:
        unresolved = stage.get("unresolved_stages") or ()
        names = ", ".join(str(item) for item in unresolved) if unresolved else "none"
        lines.append(
            f"- {stage.get('camera_id')}: status {stage.get('status')}; "
            f"unresolved: {names}"
        )
        for boundary in stage.get("boundaries") or ():
            lines.append(
                f"  - {boundary.get('stage')} "
                f"{_fmt_seconds((boundary.get('time_interval_seconds') or [None])[0])}"
                f"–{_fmt_seconds((boundary.get('time_interval_seconds') or [None, None])[1])} s, "
                f"confidence {boundary.get('confidence')} "
                f"({boundary.get('confidence_scale') or 'uncalibrated'})"
            )
    impact = evidence.get("impact") or {}
    lines.extend(
        [
            "",
            "### Impact",
            "",
            f"Status: {_cell(impact.get('status'))}. "
            f"Quality: {_cell(impact.get('quality'))}.",
            f"Classification: {_cell(impact.get('classification'))}.",
            f"Confidence: {_cell(impact.get('confidence'))} "
            f"({_cell(impact.get('confidence_scale'))}).",
            f"Interval ticks: {_cell(impact.get('interval_ticks'))}.",
            f"Reason: {_cell(impact.get('reason'))}.",
        ]
    )
    return "\n".join(lines)


def _gpt_section(gpt: dict[str, Any]) -> str:
    lines = [
        f"Status: **{gpt.get('status', 'unavailable')}.** {gpt.get('detail') or ''}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Provider | {_cell(gpt.get('provider'))} |",
        f"| Model | {_cell(gpt.get('model'))} |",
        f"| Image detail | {_cell(gpt.get('image_detail'))} |",
        f"| Image count | {_cell(gpt.get('image_count'))} |",
        f"| Reference version | {_cell(gpt.get('reference_version'))} |",
        f"| Validation | {_cell(gpt.get('validation_status'))} |",
        "",
        "Images, in request order:",
        "",
        "| Order | Id | Camera | Tick | Time (s) | File |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    images = gpt.get("images") or ()
    if not images:
        lines.append("| — | — | — | — | — | none recorded |")
    for image in images:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(image.get("order")),
                    _cell(image.get("image_id")),
                    _cell(image.get("camera_id")),
                    _cell(image.get("capture_tick")),
                    _fmt_seconds(image.get("timestamp_seconds")),
                    _cell(image.get("relative_file")),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            f"Validation errors: {_join(gpt.get('validation_errors'))}",
            "",
            "Accepted findings:",
            "",
            _findings_table(gpt.get("accepted_findings") or ()),
            "",
            "Rejected findings:",
            "",
            _findings_table(gpt.get("rejected_findings") or ()),
        ]
    )
    return "\n".join(lines)


def _decision_section(decision: dict[str, Any]) -> str:
    lines = [
        f"Status: **{decision.get('status', 'unavailable')}.** "
        f"{decision.get('detail') or decision.get('reason') or ''}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Decision | {_cell(decision.get('decision_type'))} |",
        f"| Selected checkpoint | {_cell(decision.get('selected_checkpoint'))} |",
        f"| Previous focus | {_cell(decision.get('previous_focus'))} |",
        f"| Previous delivery | {_cell(decision.get('previous_delivery_status'))} |",
        f"| Progress | {_cell(decision.get('progress'))} |",
        f"| Approved rule | {_cell(decision.get('rule_id'))} |",
        f"| Ranking | {_cell((decision.get('ranking') or {}).get('status'))} |",
        "",
        "Approved correction:",
        "",
        _quote(decision.get("approved_correction")),
        "",
        "Validated observation:",
        "",
        _quote(decision.get("validated_observation")),
        "",
        "Final floor text:",
        "",
        _quote(decision.get("hud_text")),
        "",
        "Final speech text:",
        "",
        _quote(decision.get("speech_text")),
        "",
        "Checkpoint ranking:",
        "",
        _checkpoint_table((decision.get("ranking") or {}).get("checkpoints") or ()),
    ]
    previous = decision.get("previous_cue")
    if previous:
        lines.extend(["", "Previous delivered cue:", "", _quote(previous)])
    return "\n".join(lines)


def _delivery_section(delivery: dict[str, Any]) -> str:
    hud = delivery.get("hud") or {}
    request = delivery.get("tts_request") or {}
    playback = delivery.get("tts_playback") or {}
    lines = [
        f"Floor: **{hud.get('status', 'unavailable')}.** {hud.get('detail') or ''}",
        "",
        "Text shown on the floor:",
        "",
        _quote(hud.get("text")),
        "",
        f"Speech request: **{request.get('status', 'unavailable')}.** "
        f"{request.get('detail') or ''}",
        "",
        f"Audio playback: **{playback.get('status', 'unavailable')}.** "
        f"{playback.get('detail') or ''}",
    ]
    if playback.get("status") == "success":
        lines.append(f"Spoken seconds: {_cell(playback.get('spoken_seconds'))}.")
    else:
        lines.append("Audio playback was not confirmed.")
    return "\n".join(lines)


def _timing_section(timing: dict[str, Any]) -> str:
    lines = [
        "| Span | Duration | Quality |",
        "| --- | --- | --- |",
    ]
    for span in timing.get("spans") or ():
        milliseconds = span.get("milliseconds")
        shown = "—" if milliseconds is None else f"{milliseconds} ms"
        lines.append(
            f"| {_cell(span.get('label'))} | {shown} | {_cell(span.get('quality'))} |"
        )
    if timing.get("simulated_provider"):
        lines.extend(
            [
                "",
                "GPT latency is estimated. The provider delay was simulated.",
            ]
        )
    stages = timing.get("evidence_stages") or ()
    if stages:
        lines.extend(
            [
                "",
                "Evidence stages:",
                "",
                "| Stage | Duration | Quality |",
                "| --- | --- | --- |",
            ]
        )
        for stage in stages:
            milliseconds = stage.get("milliseconds")
            shown = "—" if milliseconds is None else f"{milliseconds} ms"
            lines.append(
                f"| {_cell(stage.get('name'))} | {shown} | {_cell(stage.get('quality'))} |"
            )
    return "\n".join(lines)


def _transmission(frame: dict[str, Any]) -> str:
    value = frame.get("transmission")
    if value == "sent":
        return "sent"
    if value == "not_sent":
        return "NOT SENT"
    if value == "missing" and frame.get("requested"):
        return "missing; was in the model request"
    if value == "missing":
        return "missing"
    return "—"


def _findings_table(findings: list[Any]) -> str:
    if not findings:
        return "None."
    lines = [
        "| Checkpoint | Status | Severity | Certainty | Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in findings:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(finding.get("checkpoint_id")),
                    _cell(finding.get("status")),
                    _cell(finding.get("severity")),
                    _cell(finding.get("certainty")),
                    _cell(finding.get("exclusion_reason") or finding.get("reason")),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _checkpoint_table(rows: list[Any]) -> str:
    if not rows:
        return "No checkpoint ranking was recorded."
    lines = [
        "| Checkpoint | Eligible | Role | Status | Severity | Certainty | Why excluded |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(row.get("checkpoint_id")),
                    "yes" if row.get("eligible") else "no",
                    _cell(row.get("rank_role")),
                    _cell(row.get("status")),
                    _cell(row.get("severity")),
                    _cell(row.get("certainty")),
                    _cell(row.get("exclusion_reason")),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _bullet(title: str, items: list[Any] | tuple[Any, ...]) -> str:
    if not items:
        return f"{title}: none."
    lines = [f"{title}:", ""]
    lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _quote(value: Any) -> str:
    if value is None or value == "":
        return "_None._"
    text = str(value).replace("\n", "\n> ")
    return f"> {text}"


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return _cell(value)


def _join(values: Any) -> str:
    if not values:
        return "none"
    return "; ".join(str(item) for item in values)
