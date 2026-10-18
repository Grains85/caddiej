"""Read a saved SwingEvidencePackage. Image bytes stay on disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_evidence_package(path: Path) -> Any:
    """Load `evidence_package.json`, or that file inside a directory."""

    file_path = path
    if path.is_dir():
        file_path = path / "evidence_package.json"
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evidence package must be an object")
    return _package(payload)


def _package(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import (
        CameraClockComparison,
        SwingEvidencePackage,
    )

    return SwingEvidencePackage(
        schema_version=str(payload["schema_version"]),
        shot_id=str(payload["shot_id"]),
        status=str(payload["status"]),
        provenance=_provenance(payload["provenance"]),
        cameras=tuple(str(item) for item in payload["cameras"]),
        image_budget=int(payload["image_budget"]),
        selected_frames=tuple(
            _evidence_frame(item) for item in payload["selected_frames"]
        ),
        stage_estimates=tuple(_stage(item) for item in payload["stage_estimates"]),
        impact_estimate=_impact(payload["impact_estimate"]),
        synchronization=CameraClockComparison(
            matched_ticks=int(payload["synchronization"]["matched_ticks"]),
            median_abs_delta_seconds=payload["synchronization"][
                "median_abs_delta_seconds"
            ],
            max_abs_delta_seconds=payload["synchronization"]["max_abs_delta_seconds"],
            sufficient=bool(payload["synchronization"]["sufficient"]),
            note=str(payload["synchronization"]["note"]),
        ),
        fallback=str(payload["fallback"]),
        raw_capture_preserved=bool(payload["raw_capture_preserved"]),
        allocation=None,
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        errors=tuple(str(item) for item in payload.get("errors") or ()),
    )


def _provenance(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import Provenance

    return Provenance(
        stage=str(payload["stage"]),
        producer=str(payload["producer"]),
        producer_version=str(payload["producer_version"]),
        created_at=payload.get("created_at"),
    )


def _reference(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import FrameReference

    return FrameReference(
        camera_id=str(payload["camera_id"]),
        relative_file=str(payload["relative_file"]),
        capture_tick=int(payload["capture_tick"]),
        timestamp_seconds=float(payload["timestamp_seconds"]),
        sha256=payload.get("sha256"),
        width=payload.get("width"),
        height=payload.get("height"),
    )


def _evidence_frame(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import EvidenceFrame

    return EvidenceFrame(
        reference=_reference(payload["reference"]),
        order=int(payload["order"]),
        selection_reason=str(payload["selection_reason"]),
        stage=payload.get("stage"),
    )


def _stage(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import (
        HandExcursion,
        ImpactObservation,
        LandmarkContinuity,
        MissingInterval,
        StageParameters,
        SwingStageResult,
        TrackingSummary,
    )

    parameters = payload["parameters"]
    hand = payload["hand_excursion"]
    tracking = payload.get("tracking")
    return SwingStageResult(
        schema_version=str(payload["schema_version"]),
        shot_id=str(payload["shot_id"]),
        status=str(payload["status"]),
        provenance=_provenance(payload["provenance"]),
        camera_id=str(payload["camera_id"]),
        clock=str(payload["clock"]),
        handedness=str(payload["handedness"]),
        orientation=str(payload["orientation"]),
        confidence_scale=str(payload["confidence_scale"]),
        parameters=StageParameters(**parameters),
        boundaries=tuple(_boundary(item) for item in payload["boundaries"]),
        candidates=tuple(_boundary(item) for item in payload.get("candidates") or ()),
        unresolved_stages=tuple(
            str(item) for item in payload.get("unresolved_stages") or ()
        ),
        tracking=None
        if not tracking
        else TrackingSummary(
            frame_count=int(tracking["frame_count"]),
            pose_detected_frames=int(tracking["pose_detected_frames"]),
            pose_detection_rate=float(tracking["pose_detection_rate"]),
            usable_hand_frames=int(tracking["usable_hand_frames"]),
            both_wrists_frames=int(tracking["both_wrists_frames"]),
            continuity=tuple(
                LandmarkContinuity(**item) for item in tracking.get("continuity") or ()
            ),
            missing_landmark_intervals=tuple(
                MissingInterval(
                    landmark=str(item["landmark"]),
                    time_interval_seconds=(
                        float(item["time_interval_seconds"][0]),
                        float(item["time_interval_seconds"][1]),
                    ),
                    ticks=tuple(int(tick) for tick in item["ticks"]),
                    frame_count=int(item["frame_count"]),
                )
                for item in tracking.get("missing_landmark_intervals") or ()
            ),
        ),
        hand_excursion=HandExcursion(
            observed=bool(hand["observed"]),
            rise_torso_lengths=hand["rise_torso_lengths"],
            fall_torso_lengths=hand["fall_torso_lengths"],
            peak_time_seconds=hand["peak_time_seconds"],
            peak_tick=hand["peak_tick"],
            wrists_at_peak=hand["wrists_at_peak"],
            gates_passed=bool(hand["gates_passed"]),
            reversal=bool(hand["reversal"]),
        ),
        impact=ImpactObservation(
            supported=bool(payload["impact"]["supported"]),
            reason=str(payload["impact"]["reason"]),
        ),
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        errors=tuple(str(item) for item in payload.get("errors") or ()),
    )


def _boundary(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import ConfidenceComponent, StageBoundary

    interval = payload["time_interval_seconds"]
    return StageBoundary(
        stage=str(payload["stage"]),
        time_interval_seconds=(float(interval[0]), float(interval[1])),
        confidence=float(payload["confidence"]),
        confidence_scale=str(payload["confidence_scale"]),
        evidence_ticks=tuple(int(tick) for tick in payload["evidence_ticks"]),
        method=str(payload["method"]),
        components=tuple(
            ConfidenceComponent(name=str(item["name"]), value=float(item["value"]))
            for item in payload.get("components") or ()
        ),
    )


def _impact(payload: dict[str, Any]) -> Any:
    from caddiej.swing_evidence.contracts import ImpactEstimate, RejectedImpactCandidate

    ticks = payload.get("contact_interval_ticks")
    seconds = payload.get("contact_interval_seconds")
    ball = payload.get("normalized_ball")
    return ImpactEstimate(
        schema_version=str(payload["schema_version"]),
        shot_id=str(payload["shot_id"]),
        status=str(payload["status"]),
        provenance=_provenance(payload["provenance"]),
        classification=str(payload["classification"]),
        reason=str(payload["reason"]),
        confidence=payload.get("confidence"),
        confidence_scale=str(payload["confidence_scale"]),
        method=str(payload["method"]),
        camera_id=payload.get("camera_id"),
        contact_interval_ticks=None
        if ticks is None
        else (int(ticks[0]), int(ticks[1])),
        contact_interval_seconds=(
            None if seconds is None else (float(seconds[0]), float(seconds[1]))
        ),
        supporting_ticks=tuple(
            int(tick) for tick in payload.get("supporting_ticks") or ()
        ),
        normalized_ball=None if ball is None else (float(ball[0]), float(ball[1])),
        rejected_candidates=tuple(
            RejectedImpactCandidate(
                normalized_x=float(item["normalized_x"]),
                normalized_y=float(item["normalized_y"]),
                reason=str(item["reason"]),
                onset_tick=item.get("onset_tick"),
            )
            for item in payload.get("rejected_candidates") or ()
        ),
        camera_estimates=tuple(
            _impact(item) for item in payload.get("camera_estimates") or ()
        ),
        parameters=tuple(
            (str(name), value) for name, value in payload.get("parameters") or ()
        ),
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
    )
