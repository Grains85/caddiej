"""Build a SwingEvidencePackage from a capture's diagnostic timeline.

The sampler, the stage detector, the impact estimator, and `choose_frames`
live in `caddiej.swing_evidence`. This module decides when to call them and
when a missing pose model means the stages stay unresolved. It does not
download that model and it does not choose frames of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from caddiej.swing_evidence.assemble import ShotSuperseded as AssembleSuperseded
from caddiej.swing_evidence.assemble import assemble_shot
from caddiej.swing_evidence.contracts import to_plain

PACKAGE_NAME = "evidence_package.json"


class ShotSuperseded(Exception):
    """A newer shot arrived before this timeline was finished."""


def timeline_available(directory: Path) -> bool:
    """True when this shot still has a diagnostic timeline to read."""

    payload = _metadata(directory)
    timeline = payload.get("diagnostic_timeline") if payload else None
    return isinstance(timeline, dict) and bool(timeline.get("cameras"))


def same_capture(directory: Path, capture_id: str) -> bool:
    """True when this directory is still the shot `capture_id` was written to."""

    payload = _metadata(directory)
    return payload is not None and payload.get("capture_id") == capture_id


def timeline_retained(directory: Path) -> bool:
    """True when the operator asked for the full timeline to stay on disk."""

    payload = _metadata(directory)
    timeline = payload.get("diagnostic_timeline") if payload else None
    return isinstance(timeline, dict) and bool(timeline.get("retain_full"))


def build_live_package(
    directory: Path,
    *,
    shot_id: str,
    budget: int,
    preset: str,
    cancelled: Callable[[], bool] | None = None,
    timings: dict[str, float] | None = None,
) -> Any:
    """The same package the offline command builds, for this shot id and budget."""

    try:
        return assemble_shot(
            directory,
            budget=budget,
            preset=preset,
            shot_id=shot_id,
            allow_pose_download=False,
            timings=timings,
            cancelled=cancelled,
        )
    except AssembleSuperseded as exc:
        raise ShotSuperseded() from exc


def write_package(directory: Path, package: Any) -> Path:
    """Store the package beside the shot. Image bytes stay in the timeline."""

    path = directory / PACKAGE_NAME
    path.write_text(
        json.dumps(to_plain(package), indent=2),
        encoding="utf-8",
    )
    return path


def release_unselected(directory: Path, package: Any) -> int:
    """Delete diagnostic frames the package did not keep.

    Selected frames stay, because the package points at them. A file that is
    still open is left for a later prune. A timeline marked `retain_full` is
    not touched.
    """

    if timeline_retained(directory):
        return 0
    kept = {
        str(frame.reference.relative_file).replace("\\", "/")
        for frame in package.selected_frames
    }
    root = directory / "diagnostic"
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in kept:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _metadata(directory: Path) -> dict[str, Any] | None:
    path = directory / "metadata.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
