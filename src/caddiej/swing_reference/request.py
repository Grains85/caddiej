"""Build an image-bearing assessment request from a SwingEvidencePackage.

The manifest names each attached image. The bytes travel separately, in the
same order, with the same image id. A path is not sent in place of a picture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caddiej.coaching.provider import AdviceRequest, ImageInput
from caddiej.swing_reference.model import SwingReference

_ACCEPTED_STAGE = frozenset({"ok", "partial"})
_IMPACT_ESTIMATED = "estimated"
_PREFIX = {"swing_behind": "DTL", "swing_side": "FO"}
# A full 62-checkpoint reply is much larger than a cue. This ceiling is only
# for the reference call.
REFERENCE_MAX_OUTPUT_TOKENS = 16000

_SYSTEM = """\
You compare the attached swing images with the Swing Reference Model in the
evidence JSON. That model is version {version}. Read its terminology and its
instructions before you score a checkpoint. Use only the statuses, problem
keys and checkpoint ids that model defines.

Unknown is not correct. If the camera, stage, pose, sequence or player context
is missing, mark the checkpoint unknown. Do not invent an observation to fill
the list. A filename is not a swing phase. An accepted stage is only a broad
label. One frame cannot establish a movement the reference marks as a sequence.
Do not invent angles, pressure, or a confirmed club-ball contact. An impact
status of estimated is not contact.

DTL means camera swing_behind. FO means camera swing_side. Each attached image
is announced with an id such as DTL_001. Cite evidence only with the
camera_id, relative_file, capture_tick and timestamp_seconds listed for that
image. Do not cite an image that was not attached.

The previous note, when present, is the active focus and the cue that was
actually delivered. Score whether this swing shows that focus. Do not claim
the swing is better than a previous swing. Comparative change is unknown
unless comparable earlier images were attached, and none are attached here.

The Swing Reference Model JSON is a stable prefix, not part of this shot.
Read that prefix. Do not expect the shot JSON to repeat it.

Set coaching to null. Do not write a correction or a cue. Set positive_feedback
to null unless you copy one stored positive_feedback string exactly. Do not use
any older fault catalog. Cite checkpoint ids, problem keys and rule ids from
the reference. Do not restate the reference.
"""


@dataclass(frozen=True, slots=True)
class PlayerContext:
    """Known player facts. Unknown stays unknown; the reference default is not used."""

    club_type: str = "unknown"
    shot_type: str = "unknown"
    distance_yards: float | None = None
    handedness: str = "unknown"
    target_line: str = "unknown"
    evidence_context: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "club_type": self.club_type,
            "shot_type": self.shot_type,
            "distance_yards": self.distance_yards,
            "handedness": self.handedness,
            "target_line": self.target_line,
            "evidence_context": self.evidence_context,
        }


@dataclass(frozen=True, slots=True)
class PreviousNote:
    """The only previous-shot text the next assessment is given."""

    active_focus: str
    previous_cue: str | None
    previous_observation: str | None
    delivery_status: str
    schema_version: str = "1"
    previous_status: str | None = None
    previous_severity: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "active_focus": self.active_focus,
            "previous_cue": self.previous_cue,
            "previous_observation": self.previous_observation,
            "delivery_status": self.delivery_status,
            "previous_status": self.previous_status,
            "previous_severity": self.previous_severity,
        }


@dataclass(frozen=True, slots=True)
class AttachedImage:
    image_id: str
    camera_id: str
    relative_file: str
    capture_tick: int
    timestamp_seconds: float
    stage: str | None
    sha256: str | None
    path: Path
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    shot_id: str
    reference_version: str
    manifest: dict[str, Any]
    images: tuple[AttachedImage, ...]
    system: str
    instructions: str
    schema: dict[str, Any]
    warnings: tuple[str, ...]
    reference_prefix: str

    def advice_request(
        self,
        *,
        max_output_tokens: int = REFERENCE_MAX_OUTPUT_TOKENS,
        timeout_seconds: float = 30.0,
        image_detail: str | None = None,
    ) -> AdviceRequest:
        """The provider request. Image bytes are on `images`, not in the JSON."""

        return AdviceRequest(
            system=self.system,
            instructions=self.instructions,
            evidence=json.dumps(self.manifest, ensure_ascii=False),
            schema=_provider_schema(self.schema),
            images=tuple(
                ImageInput(
                    data=image.data,
                    media_type=image.media_type,
                    label=_label(image),
                    frame_id=image.image_id,
                    detail=image_detail,
                )
                for image in self.images
            ),
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            schema_name="swing_assessment_v2",
            strict_schema=False,
            prefix=self.reference_prefix,
        )


def build_request(
    package: Any,
    shot_dir: Path,
    reference: SwingReference,
    *,
    context: PlayerContext | None = None,
    previous: PreviousNote | None = None,
) -> PreparedRequest:
    player = context or PlayerContext()
    accepted = _accepted_stages(package)
    ordered = sorted(
        package.selected_frames,
        key=lambda frame: (
            frame.reference.timestamp_seconds,
            frame.reference.camera_id,
            frame.reference.capture_tick,
        ),
    )
    ids = _image_ids(ordered)
    warnings = [str(item) for item in package.warnings]
    images: list[AttachedImage] = []
    manifest_frames: list[dict[str, Any]] = []
    for frame in ordered:
        reference_frame = frame.reference
        image_id = ids[_identity(reference_frame)]
        path = (shot_dir / reference_frame.relative_file).resolve()
        if shot_dir.resolve() not in path.parents or not path.is_file():
            warnings.append(f"{image_id} file is missing; it is not attached")
            continue
        data = path.read_bytes()
        if not data:
            warnings.append(f"{image_id} file is empty; it is not attached")
            continue
        stage = _stage_name(frame, accepted)
        attached = AttachedImage(
            image_id=image_id,
            camera_id=str(reference_frame.camera_id),
            relative_file=str(reference_frame.relative_file),
            capture_tick=int(reference_frame.capture_tick),
            timestamp_seconds=float(reference_frame.timestamp_seconds),
            stage=stage,
            sha256=reference_frame.sha256,
            path=path,
            data=data,
            media_type=_media_type(path),
        )
        images.append(attached)
        manifest_frames.append(_manifest_frame(attached))
    if not any(image.camera_id == "swing_behind" for image in images):
        warnings.append("no swing_behind frames attached")
    if not any(image.camera_id == "swing_side" for image in images):
        warnings.append("no swing_side frames attached")
    manifest: dict[str, Any] = {
        "schema_name": "swing_assessment_v2",
        "reference_version": reference.version,
        "shot_id": package.shot_id,
        "context": player.as_dict(),
        "frames": manifest_frames,
        "image_index": [
            {
                "image_id": image.image_id,
                "camera_id": image.camera_id,
                "relative_file": image.relative_file,
                "capture_tick": image.capture_tick,
                "timestamp_seconds": image.timestamp_seconds,
            }
            for image in images
        ],
        "impact": _impact(package),
        "stage_uncertainties": _stage_uncertainties(package),
        "previous_context": _previous_context(previous),
        "warnings": warnings,
        "reference": {
            "version": reference.version,
            "schema_name": reference.raw.get("schema_version"),
            "placement": "stable_prefix",
        },
    }
    return PreparedRequest(
        shot_id=str(package.shot_id),
        reference_version=reference.version,
        manifest=manifest,
        images=tuple(images),
        system=_SYSTEM.format(version=reference.version),
        instructions=_instructions(reference),
        schema=reference.schema,
        warnings=tuple(warnings),
        reference_prefix=reference_prefix(reference),
    )


def reference_prefix(reference: SwingReference) -> str:
    """The full reference, serialized once so every shot shares the same prefix.

    Key order is sorted so two processes produce the same bytes. The shot,
    the images and the previous note are not in this string.
    """

    body = json.dumps(
        reference.raw,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"Swing Reference Model {reference.version}. "
        "This prefix is the full reference and is identical for every shot.\n"
        + body
    )


def _previous_context(previous: PreviousNote | None) -> dict[str, Any] | None:
    """Send the cue only when delivery confirms the golfer heard it."""

    if previous is None:
        return None
    if previous.delivery_status == "delivered":
        return previous.as_dict()
    return PreviousNote(
        active_focus=previous.active_focus,
        previous_cue=None,
        previous_observation=previous.previous_observation,
        delivery_status=previous.delivery_status,
        schema_version=previous.schema_version,
    ).as_dict()


def _instructions(reference: SwingReference) -> str:
    lines = [
        f"Reference version: {reference.version}.",
        "Terminology:",
        json.dumps(reference.terminology, ensure_ascii=False, indent=2),
        "Reference instructions:",
    ]
    lines.extend(reference.instructions)
    lines.append(
        "Return every checkpoint once. Copy no correction. Leave coaching null."
    )
    return "\n".join(lines)


def _image_ids(frames: list[Any]) -> dict[tuple[Any, ...], str]:
    grouped: dict[str, list[Any]] = {}
    for frame in frames:
        grouped.setdefault(str(frame.reference.camera_id), []).append(frame)
    assigned: dict[tuple[Any, ...], str] = {}
    for camera_id, items in grouped.items():
        prefix = _PREFIX.get(camera_id, camera_id)
        ordered = sorted(
            items,
            key=lambda frame: (
                frame.reference.timestamp_seconds,
                frame.reference.capture_tick,
                frame.reference.relative_file,
            ),
        )
        for index, frame in enumerate(ordered, start=1):
            assigned[_identity(frame.reference)] = f"{prefix}_{index:03d}"
    return assigned


def _identity(reference: Any) -> tuple[Any, ...]:
    return (
        reference.camera_id,
        reference.relative_file,
        reference.capture_tick,
        reference.timestamp_seconds,
    )


def _accepted_stages(package: Any) -> dict[str, frozenset[str]]:
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


def _stage_name(frame: Any, accepted: dict[str, frozenset[str]]) -> str | None:
    if not frame.stage:
        return None
    allowed = accepted.get(frame.reference.camera_id, frozenset())
    if frame.stage not in allowed:
        return None
    return str(frame.stage)


def _manifest_frame(image: AttachedImage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "image_id": image.image_id,
        "camera_id": image.camera_id,
        "relative_file": image.relative_file,
        "capture_tick": image.capture_tick,
        "timestamp_seconds": image.timestamp_seconds,
    }
    if image.sha256:
        payload["sha256"] = image.sha256
    if image.stage:
        payload["stage"] = image.stage
    return payload


def _impact(package: Any) -> dict[str, Any]:
    estimate = package.impact_estimate
    payload: dict[str, Any] = {
        "status": estimate.status,
        "classification": estimate.classification,
        "reason": estimate.reason,
    }
    if estimate.status == _IMPACT_ESTIMATED:
        if estimate.contact_interval_ticks is not None:
            payload["contact_interval_ticks"] = list(estimate.contact_interval_ticks)
        if estimate.contact_interval_seconds is not None:
            payload["contact_interval_seconds"] = list(
                estimate.contact_interval_seconds
            )
    return payload


def _stage_uncertainties(package: Any) -> list[str]:
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


def _label(image: AttachedImage) -> str:
    stage = image.stage or "stage unresolved"
    return (
        f"{image.camera_id} tick {image.capture_tick} "
        f"t={image.timestamp_seconds} {stage}"
    )


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    return "application/octet-stream"


def _provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    copy = dict(schema)
    copy.pop("$schema", None)
    return copy
