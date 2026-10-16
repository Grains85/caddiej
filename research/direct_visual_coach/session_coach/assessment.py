"""Check a visual assessment before the coaching engine reads it."""

from __future__ import annotations

from typing import Any

from contracts import (
    CHANGE_SINCE_PREVIOUS,
    CONFIDENCE_SCALE,
    CURRENT_FOCUS_STATUSES,
    FOLLOW_UP_PROGRESS,
    STAGE_CORRESPONDENCE,
    AssessmentFinding,
    FocusFollowUp,
    FrameReference,
    Provenance,
    VisualAssessmentResult,
)

from session_coach.models import COMPARISONS

WEAKNESS = "weakness"
STRENGTH = "strength"
CAUSAL = "causal_hypothesis"
KINDS = frozenset({WEAKNESS, STRENGTH, CAUSAL})


def validate_assessment(result: VisualAssessmentResult) -> tuple[str, ...]:
    """Return problems. An empty tuple means the engine may read the result."""

    problems: list[str] = []
    if not result.shot_id:
        problems.append("shot_id is required")
    if result.no_proven_finding and any(
        item.kind == WEAKNESS for item in result.findings
    ):
        problems.append("no_proven_finding cannot carry a weakness")
    for finding in (*result.findings, *result.causal_hypotheses):
        problems.extend(_finding_problems(finding))
    if result.focus_follow_up is not None:
        problems.extend(_follow_up_problems(result.focus_follow_up))
    return tuple(problems)


def _finding_problems(finding: AssessmentFinding) -> list[str]:
    problems: list[str] = []
    label = finding.fault_id or finding.kind
    if finding.kind not in KINDS:
        problems.append(f"{label} kind is not a finding type")
    if finding.kind == CAUSAL and not finding.unverified:
        problems.append(f"{label} causal hypothesis must be unverified")
    if finding.kind != CAUSAL and finding.unverified:
        problems.append(f"{label} observation is marked unverified")
    if finding.comparison is not None and finding.comparison not in COMPARISONS:
        problems.append(f"{label} comparison is not a known relation")
    if not 0.0 <= finding.confidence <= 1.0:
        problems.append(f"{label} confidence is outside 0 to 1")
    if finding.confidence_scale != CONFIDENCE_SCALE:
        problems.append(f"{label} confidence scale is not the uncalibrated heuristic")
    if (
        finding.kind != STRENGTH
        and not finding.evidence_frames
        and finding.kind != CAUSAL
    ):
        problems.append(f"{label} weakness has no evidence frame")
    for frame in finding.evidence_frames:
        if not frame.relative_file or frame.capture_tick < 0:
            problems.append(f"{label} evidence reference is incomplete")
        if frame.relative_file.startswith(("/", "\\")) or ".." in frame.relative_file:
            problems.append(f"{label} evidence path is not a relative reference")
    return problems


def _follow_up_problems(follow: FocusFollowUp) -> list[str]:
    problems: list[str] = []
    if not follow.previous_focus_id:
        problems.append("follow-up needs a previous focus")
    if follow.current_focus_status not in CURRENT_FOCUS_STATUSES:
        problems.append("current focus status is unknown")
    if follow.change_since_previous not in CHANGE_SINCE_PREVIOUS:
        problems.append("change since previous is unknown")
    if follow.progress_assessment not in FOLLOW_UP_PROGRESS:
        problems.append("progress assessment is unknown")
    if follow.stage_correspondence not in STAGE_CORRESPONDENCE:
        problems.append("stage correspondence is unknown")
    if follow.previous_image_count < 0 or follow.previous_image_count > 2:
        problems.append("previous image count must be from 0 to 2")
    for frame in follow.evidence_frames:
        if not frame.relative_file or frame.capture_tick < 0:
            problems.append("follow-up evidence reference is incomplete")
        if frame.relative_file.startswith(("/", "\\")) or ".." in frame.relative_file:
            problems.append("follow-up evidence path is not a relative reference")
    return problems


def assessment_from_plain(payload: dict[str, Any]) -> VisualAssessmentResult:
    provenance = payload["provenance"]
    return VisualAssessmentResult(
        schema_version=str(payload["schema_version"]),
        shot_id=str(payload["shot_id"]),
        status=str(payload["status"]),
        provenance=Provenance(
            str(provenance["stage"]),
            str(provenance["producer"]),
            str(provenance["producer_version"]),
            provenance.get("created_at"),
        ),
        findings=tuple(_finding(item) for item in payload.get("findings") or []),
        no_proven_finding=bool(payload["no_proven_finding"]),
        stage_result_schema_version=str(
            payload.get("stage_result_schema_version") or ""
        ),
        stage_uncertainties=tuple(payload.get("stage_uncertainties") or ()),
        causal_hypotheses=tuple(
            _finding(item) for item in payload.get("causal_hypotheses") or []
        ),
        sequence=payload.get("sequence"),
        warnings=tuple(payload.get("warnings") or ()),
        errors=tuple(payload.get("errors") or ()),
        focus_follow_up=_follow_up(payload.get("focus_follow_up")),
    )


def _finding(payload: dict[str, Any]) -> AssessmentFinding:
    return AssessmentFinding(
        kind=str(payload["kind"]),
        fault_id=payload.get("fault_id"),
        confidence=float(payload["confidence"]),
        confidence_scale=str(payload["confidence_scale"]),
        evidence_frames=tuple(
            _frame(item) for item in payload.get("evidence_frames") or []
        ),
        relied_on_stages=tuple(payload.get("relied_on_stages") or ()),
        note=str(payload.get("note") or ""),
        limitations=str(payload.get("limitations") or ""),
        unverified=bool(payload.get("unverified") or False),
        comparison=payload.get("comparison"),
    )


def _follow_up(payload: Any) -> FocusFollowUp | None:
    if not payload:
        return None
    return FocusFollowUp(
        previous_focus_id=str(payload["previous_focus_id"]),
        current_focus_status=str(payload["current_focus_status"]),
        change_since_previous=str(payload["change_since_previous"]),
        progress_assessment=str(payload["progress_assessment"]),
        current_focus_observation=str(payload.get("current_focus_observation") or ""),
        evidence_frames=tuple(
            _frame(item) for item in payload.get("evidence_frames") or []
        ),
        limitations=str(payload.get("limitations") or ""),
        previous_image_count=int(payload.get("previous_image_count") or 0),
        stage_correspondence=str(payload.get("stage_correspondence") or "unavailable"),
    )


def _frame(payload: dict[str, Any]) -> FrameReference:
    return FrameReference(
        camera_id=str(payload["camera_id"]),
        relative_file=str(payload["relative_file"]),
        capture_tick=int(payload["capture_tick"]),
        timestamp_seconds=float(payload["timestamp_seconds"]),
        sha256=payload.get("sha256"),
        width=payload.get("width"),
        height=payload.get("height"),
    )
