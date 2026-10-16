"""Optional previous-shot frames. The default is none.

This does not align swings. A frame is kept only when the caller already
linked it to the previous assessment, the camera matches the current package,
and the stage correspondence is reliable.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import STAGE_CORRESPONDENCE_RELIABLE, FrameReference

MAX_PREVIOUS_IMAGES = 2


@dataclass(frozen=True, slots=True)
class PreviousImageCandidate:
    reference: FrameReference
    stage_correspondence: str
    linked_finding_id: str | None


def select_previous_images(
    candidates: tuple[PreviousImageCandidate, ...] | list[PreviousImageCandidate],
    *,
    requested: bool,
    limit: int = 0,
    current_cameras: tuple[str, ...] = (),
) -> tuple[tuple[FrameReference, ...], str]:
    """Return up to two frames, plus why any were left out."""

    if not requested or limit <= 0:
        return (), "previous images not requested"
    kept: list[FrameReference] = []
    skipped_stage = False
    skipped_camera = False
    cap = min(limit, MAX_PREVIOUS_IMAGES)
    for candidate in candidates:
        if len(kept) >= cap:
            break
        frame = candidate.reference
        if candidate.stage_correspondence != STAGE_CORRESPONDENCE_RELIABLE:
            skipped_stage = True
            continue
        if not candidate.linked_finding_id:
            continue
        if current_cameras and frame.camera_id not in current_cameras:
            skipped_camera = True
            continue
        kept.append(frame)
    if kept:
        return tuple(kept), ""
    if skipped_stage:
        return (), "stage correspondence is not reliable"
    if skipped_camera:
        return (), "no comparable camera"
    return (), "no linked previous frame"
