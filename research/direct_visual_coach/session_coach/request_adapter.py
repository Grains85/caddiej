"""Turn a package and a small previous note into an assessment request.

The request names files. It does not read them, and it does not call a model.
The frozen visual classifier is left unchanged.
"""

from __future__ import annotations

from contracts import (
    IMPACT_ESTIMATED,
    STATUS_OK,
    STATUS_PARTIAL,
    EvidenceFrame,
    SwingEvidencePackage,
)

from session_coach.models import PreviousCoachingContext
from session_coach.previous_images import PreviousImageCandidate, select_previous_images

_ACCEPTED_STAGE = frozenset({STATUS_OK, STATUS_PARTIAL})

_INSTRUCTIONS = """\
Look at the current frames first. The previous note, when present, is only a
reminder of the active focus and the cue that was actually given. Disagree
with that note when the current frames do not support it. Do not invent a
fault so the note stays true, and do not invent an improvement because the
note asked for one.

Say whether the requested movement is visible in this swing. Separately, say
whether this swing changed since the previous one. Without previous frames
and reliable stage correspondence, the change is unknown. A missing fault, a
lower confidence, and a ball flight are not that change. Do not give a
percentage.

Do not write a cue, a drill, or any coaching advice. Report what is visible,
the frame references that support it, and the uncertainty.
"""


def build_assessment_request(
    package: SwingEvidencePackage,
    previous: PreviousCoachingContext | None = None,
    previous_images: tuple[PreviousImageCandidate, ...] = (),
    *,
    compare_previous: bool = False,
) -> dict[str, object]:
    frames = _frames(package)
    attached, image_note = select_previous_images(
        previous_images,
        requested=compare_previous,
        limit=2 if compare_previous else 0,
        current_cameras=package.cameras,
    )
    warnings = list(package.warnings)
    if image_note and compare_previous:
        warnings.append(image_note)
    if len(previous_images) > 2 and compare_previous:
        warnings.append("previous images are capped at 2")
    request: dict[str, object] = {
        "schema_name": "visual_assessment_v1",
        "shot_id": package.shot_id,
        "instructions": _INSTRUCTIONS,
        "frames": frames,
        "impact": _impact(package),
        "stage_uncertainties": _stage_uncertainties(package),
        "previous_context": None if previous is None else _context(previous),
        "previous_images": [_reference(frame) for frame in attached],
        "warnings": warnings,
    }
    return request


def _frames(package: SwingEvidencePackage) -> list[dict[str, object]]:
    ordered = sorted(
        package.selected_frames,
        key=lambda frame: (
            frame.reference.timestamp_seconds,
            frame.reference.camera_id,
            frame.reference.capture_tick,
        ),
    )
    accepted = _accepted_stages(package)
    return [_frame(frame, accepted) for frame in ordered]


def _accepted_stages(package: SwingEvidencePackage) -> dict[str, frozenset[str]]:
    accepted: dict[str, frozenset[str]] = {}
    for estimate in package.stage_estimates:
        if estimate.status not in _ACCEPTED_STAGE:
            continue
        unresolved = set(estimate.unresolved_stages)
        names = {
            boundary.stage
            for boundary in estimate.boundaries
            if boundary.stage not in unresolved
        }
        accepted[estimate.camera_id] = frozenset(names)
    return accepted


def _frame(
    frame: EvidenceFrame, accepted: dict[str, frozenset[str]]
) -> dict[str, object]:
    reference = frame.reference
    payload: dict[str, object] = {
        "camera_id": reference.camera_id,
        "relative_file": reference.relative_file,
        "capture_tick": reference.capture_tick,
        "timestamp_seconds": reference.timestamp_seconds,
        "order": frame.order,
    }
    allowed = accepted.get(reference.camera_id, frozenset())
    if frame.stage and frame.stage in allowed:
        payload["stage"] = frame.stage
    return payload


def _impact(package: SwingEvidencePackage) -> dict[str, object]:
    estimate = package.impact_estimate
    payload: dict[str, object] = {
        "status": estimate.status,
        "classification": estimate.classification,
        "reason": estimate.reason,
    }
    if estimate.status == IMPACT_ESTIMATED:
        payload["contact_interval_ticks"] = estimate.contact_interval_ticks
        payload["contact_interval_seconds"] = estimate.contact_interval_seconds
    return payload


def _stage_uncertainties(package: SwingEvidencePackage) -> list[str]:
    names: list[str] = []
    for estimate in package.stage_estimates:
        for stage in estimate.unresolved_stages:
            if stage not in names:
                names.append(stage)
        if estimate.status not in _ACCEPTED_STAGE:
            label = f"{estimate.camera_id}:{estimate.status}"
            if label not in names:
                names.append(label)
    return names


def _context(previous: PreviousCoachingContext) -> dict[str, object]:
    return {
        "schema_version": previous.schema_version,
        "active_focus": previous.active_focus,
        "previous_cue": previous.previous_cue,
        "previous_observation": previous.previous_observation,
        "delivery_status": previous.delivery_status,
    }


def _reference(frame) -> dict[str, object]:
    return {
        "camera_id": frame.camera_id,
        "relative_file": frame.relative_file,
        "capture_tick": frame.capture_tick,
        "timestamp_seconds": frame.timestamp_seconds,
    }
