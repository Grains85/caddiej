"""Reading a finished capture's stills back off disk.

Separate from the service that wrote them, and deliberately the only way out.
The service holds a `SwingCapture`, which is paths and pixel sizes; a consumer
that wants the bytes asks here rather than being handed megabytes of JPEG it
may never look at.

Nothing in this module knows what the bytes are for.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from caddiej.domain.cues import PanelView
from caddiej.domain.media import SwingCameraCapture, SwingCapture, SwingFrame
from caddiej.swing_capture.phases import frame_detail
from caddiej.swing_capture.select import package_quota, take_evenly
from caddiej.swing_capture.store import (
    frame_files,
    frame_id,
    update_capture_metadata,
)

logger = logging.getLogger(__name__)

# A still that is larger than this is not a still. The capture directory is
# CaddieJ's own, but reading it straight into a model request is a good place
# to refuse to be surprised.
_MAX_STILL_BYTES = 8 * 1024 * 1024


IMPACT_PANEL_ID = "impact"
"""ProTee's top-down impact graphic, which is a panel like any other here and
is named because two other modules need to recognise its frames: the coach
image resolver, which prefers it when a line's evidence is the metrics grid,
and the label table below."""


def image_label(camera: SwingCameraCapture) -> str:
    """The words the model is shown beside this panel's stills.

    Explicit on purpose. The model is not asked to guess the angle from the
    pixels, and a behind observation is not allowed to be read from a side
    picture.
    """

    if camera.view is PanelView.BEHIND or camera.camera_id == "swing_behind":
        return "SWING_BEHIND / DOWN_THE_LINE"
    if camera.view is PanelView.SIDE or camera.camera_id == "swing_side":
        return "SWING_SIDE / FACE_ON"
    if camera.camera_id == IMPACT_PANEL_ID:
        return "IMPACT"
    if camera.camera_id == "delivery":
        return "DELIVERY"
    if camera.view is PanelView.NONE:
        return camera.camera_id.upper()
    return f"{camera.camera_id.upper()} / {camera.view.value.upper()}"


def frame_label(camera: SwingCameraCapture, frame: SwingFrame) -> str:
    """The words the model is shown beside one still: the panel, and the moment.

    The panel half is `image_label` and is a fact -- the configuration says
    which camera this is. The moment half is a guess, and says so: `~transition
    @2.9s` is CaddieJ reporting what it was aiming at and when the shutter
    went, not an assertion about what is in the picture.

    The tilde is doing real work. A model shown an unlabelled still of a
    backswing and asked about the transition will answer about the transition,
    because nothing in the request lets it say "that is not what you sent me".
    A model shown `~transition` can disagree, and a calibration that has
    drifted then surfaces as disagreement rather than as a confident rating of
    the wrong moment. The first hardware session rated the downswing `good` at
    the confidence ceiling on four shots from stills that contained no
    downswing at all.
    """

    panel = image_label(camera)
    if frame.phase is None:
        return panel
    return f"{panel} ~{frame.phase.value} @{frame.offset_seconds:.1f}s"


def frame_views(capture: SwingCapture | None) -> dict[str, PanelView]:
    """Which camera each still came from, by the id the request announced.

    Taken from the capture rather than parsed back out of the label, which
    would work and would be a string match on prose. A criterion that may only
    be read from one view needs this to be a fact: drift away from the target
    is a face-on observation and the trail shoulder moving out towards the
    ball is a down-the-line one, and a model shown the wrong camera answers
    just as confidently.
    """

    if capture is None:
        return {}
    return {
        frame_id(frame): camera.view
        for camera in capture.cameras
        for frame in camera.frames
    }


def provider_frames(
    capture: SwingCapture, limit: int
) -> tuple[tuple[SwingFrame, str], ...]:
    """The stills to send, labeled, totalling at most `limit`.

    Already-selected frames are grouped by panel. When they already fit, every
    one is kept. When they do not, the same quota the capture used prefers the
    body panels and still keeps one still of each static panel that has one.
    """

    if limit <= 0:
        return ()
    labeled = [
        (frame, frame_label(camera, frame))
        for camera in capture.cameras
        for frame in camera.frames
    ]
    if len(labeled) <= limit:
        return tuple(labeled)
    quota = package_quota(
        limit,
        tuple(
            (
                camera.camera_id,
                camera.kind.value == "static",
                len(camera.frames),
            )
            for camera in capture.cameras
        ),
    )
    chosen: list[tuple[SwingFrame, str]] = []
    for camera in capture.cameras:
        count = quota.get(camera.camera_id, 0)
        if count <= 0 or not camera.frames:
            continue
        indexes = take_evenly(tuple(range(len(camera.frames))), count)
        chosen.extend(
            (camera.frames[index], frame_label(camera, camera.frames[index]))
            for index in indexes
        )
    return tuple(chosen)


def ordered_frames(capture: SwingCapture) -> tuple[SwingFrame, ...]:
    """Every kept still, in the order a viewer should see them.

    Interleaved by time rather than grouped by camera, which keeps a shot's
    stills in the order they were taken.

    Not because the two panels are a synchronised pair. They were assumed to
    be and they are not: on every measured capture the same instant was a
    different moment of the swing in each replay (`phases` has the
    measurements). So two adjacent stills here are two
    moments of the same capture window and NOT two views of the same moment,
    and nothing may read them as a pair. This order is for a person scrolling
    through a directory. What the model is told about each still is its own
    view and its own aimed phase, and the prompt says the views are separate
    recordings.
    """

    frames = [frame for camera in capture.cameras for frame in camera.frames]
    frames.sort(key=lambda frame: (frame.offset_seconds, frame.camera_id))
    return tuple(frames)


async def read_stills(capture: SwingCapture, limit: int) -> tuple[bytes, ...]:
    """The first `limit` stills of this capture, as JPEG bytes.

    Never raises. A capture directory that has been replaced, emptied or moved
    is a reason to coach from the numbers, not a reason to lose the shot.
    """

    if limit <= 0:
        return ()
    chosen = tuple(frame for frame, _label in provider_frames(capture, limit))
    if not chosen:
        return ()
    return await asyncio.to_thread(_read, capture.directory, chosen)


async def read_labeled_stills(
    capture: SwingCapture, limit: int
) -> tuple[tuple[bytes, str, str, str, str], ...]:
    """Bytes, label, panel id, frame id and image detail, for `limit` stills.

    Never raises. A missing file is omitted; the rest of the package is sent,
    and keeps its ids, because an id names a file and not a place in a list.

    The detail is empty for every still the request's own setting is right for,
    and `"high"` for the few moments that are unreadable at a thumbnail's
    resolution. See `phases.HIGH_DETAIL_PHASES`.
    """

    if limit <= 0:
        return ()
    chosen = provider_frames(capture, limit)
    if not chosen:
        return ()
    return await asyncio.to_thread(_read_labeled, capture.directory, chosen)


METRICS_LABEL = "ALL_VALUES"
METRICS_PANEL_ID = "coach_metrics"


async def read_metrics_still(capture: SwingCapture) -> bytes | None:
    """The All Values still's JPEG bytes, or None. Never raises."""

    metrics = capture.metrics
    if metrics is None or not metrics.ok or metrics.path is None:
        return None
    path = capture.directory / metrics.path
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        logger.warning(
            "coach_metrics_still_unreadable",
            extra={"shot_trigger_id": capture.trigger_id},
            exc_info=True,
        )
        return None
    if not data or len(data) > _MAX_STILL_BYTES:
        return None
    return data


def note_provider_images(
    capture: SwingCapture,
    sent: tuple[tuple[str, str], ...],
    metadata: dict[str, object] | None = None,
    frame_ids: tuple[str, ...] = (),
) -> None:
    """Record which labeled stills were handed to the provider.

    `sent` is `(panel_id, label)` in request order. The All Values still, if
    sent, is listed with panel id `coach_metrics` and marked as the metrics
    evidence. A failure to write the note does not affect the request that
    already happened.

    `frame_ids` are the ids the request announced, and each is written down
    beside the file it resolves to. The capture is pruned a few shots later,
    but a stored line that cites `swing_side_02` should still be able to say,
    for as long as the directory exists, which picture that was.
    """

    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for panel_id, label in sent:
        counts[panel_id] = counts.get(panel_id, 0) + 1
        labels[panel_id] = label
    updates: dict[str, object] = {
        "provider_images": [
            {
                "panel_id": panel_id,
                "label": labels[panel_id],
                "sent": count,
                "metrics_evidence": panel_id == METRICS_PANEL_ID,
            }
            for panel_id, count in counts.items()
        ],
        "provider_image_count": len(sent),
        "provider_swing_frame_count": sum(
            count for panel_id, count in counts.items() if panel_id != METRICS_PANEL_ID
        ),
    }
    if frame_ids:
        files = frame_files(capture)
        updates["provider_frame_ids"] = {
            identifier: files[identifier].as_posix()
            for identifier in frame_ids
            if identifier in files
        }
    if metadata:
        updates.update(metadata)
    for directory in (capture.directory, capture.published):
        if directory is None:
            continue
        try:
            update_capture_metadata(directory, capture.trigger_id, updates)
        except OSError:
            logger.warning(
                "swing_capture_provider_note_failed",
                extra={
                    "shot_trigger_id": capture.trigger_id,
                    "swing_capture_directory": str(directory),
                },
                exc_info=True,
            )


def _read(directory: Path, frames: tuple[SwingFrame, ...]) -> tuple[bytes, ...]:
    images: list[bytes] = []
    for frame in frames:
        path = directory / frame.path
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning(
                "swing_capture_still_unreadable",
                extra={"swing_capture_camera": frame.camera_id},
                exc_info=True,
            )
            continue
        if not data or len(data) > _MAX_STILL_BYTES:
            continue
        images.append(data)
    return tuple(images)


def _read_labeled(
    directory: Path, frames: tuple[tuple[SwingFrame, str], ...]
) -> tuple[tuple[bytes, str, str, str, str], ...]:
    images: list[tuple[bytes, str, str, str, str]] = []
    for frame, label in frames:
        path = directory / frame.path
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning(
                "swing_capture_still_unreadable",
                extra={"swing_capture_camera": frame.camera_id},
                exc_info=True,
            )
            continue
        if not data or len(data) > _MAX_STILL_BYTES:
            continue
        images.append(
            (
                data,
                label,
                frame.camera_id,
                frame_id(frame),
                frame_detail(frame.phase, ""),
            )
        )
    return tuple(images)
