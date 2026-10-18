"""Shared builders for swing-reference tests. Not a golf rulebook."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from caddiej.swing_reference.model import SwingReference, load_reference
from caddiej.swing_reference.request import PlayerContext
from caddiej.swing_reference.validate import insufficient_assessment

PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "direct_visual_coach"
    / "results"
    / "evidence_pipeline"
    / "v1"
    / "20261008T191545-2-09132997"
    / "evidence_package.json"
)
SHOT_DIR = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "temporal_evidence"
    / "fresh_v2"
    / "shots"
    / "20261008T191545-2-09132997"
)

IRON = PlayerContext(
    club_type="iron",
    shot_type="stock",
    handedness="right",
    evidence_context="player_swing",
)


def reference() -> SwingReference:
    return load_reference()


def request_for(
    shot_id: str,
    frames: list[dict[str, Any]],
    context: PlayerContext | None = None,
    *,
    version: str = "2.9-timing-first",
) -> dict[str, Any]:
    player = IRON if context is None else context
    return {
        "schema_name": "swing_assessment_v2",
        "reference_version": version,
        "shot_id": shot_id,
        "context": player.as_dict(),
        "frames": frames,
        "impact": {
            "status": "unresolved",
            "classification": "unknown",
            "reason": "test",
        },
        "stage_uncertainties": [],
        "warnings": [],
    }


def frame(
    *,
    camera: str = "swing_behind",
    tick: int = 23,
    seconds: float = 2.7,
    stage: str | None = "BACKSWING",
    sha: str = "abc123",
    relative: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "camera_id": camera,
        "relative_file": relative or f"diagnostic/{camera}/frame_{tick:03d}.jpg",
        "capture_tick": tick,
        "timestamp_seconds": seconds,
        "sha256": sha,
    }
    if stage is not None:
        payload["stage"] = stage
    return payload


def assessment(
    book: SwingReference,
    shot_id: str,
    updates: dict[str, dict[str, Any]] | None = None,
    *,
    ranking: str = "insufficient_evidence",
    primary: str | None = None,
    secondary: list[str] | None = None,
    note: str = "Test ranking note.",
    coaching: dict[str, Any] | None = None,
    positive: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = insufficient_assessment(book, shot_id, IRON.as_dict())
    by_id = {item["checkpoint_id"]: item for item in payload["findings"]}
    for checkpoint_id, fields in (updates or {}).items():
        by_id[checkpoint_id].update(fields)
    payload["findings"] = [by_id[item["checkpoint_id"]] for item in payload["findings"]]
    payload["ranking_status"] = ranking
    payload["primary_checkpoint_id"] = primary
    payload["secondary_checkpoint_ids"] = secondary or []
    payload["ranking_note"] = note
    payload["coaching"] = coaching
    payload["positive_feedback"] = positive
    return payload


def deviation(
    checkpoint_id: str,
    problem: str,
    evidence: list[dict[str, Any]],
    *,
    severity: str = "clear",
    certainty: str = "high",
) -> dict[str, Any]:
    return {
        "status": "deviation",
        "problem": problem,
        "severity": severity,
        "certainty": certainty,
        "observation": f"Visible {problem} at {checkpoint_id}.",
        "evidence_frames": [
            {
                "camera_id": item["camera_id"],
                "relative_file": item["relative_file"],
                "capture_tick": item["capture_tick"],
                "timestamp_seconds": item["timestamp_seconds"],
            }
            for item in evidence
        ],
    }
