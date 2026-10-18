"""Result contracts for evidence production and, later, coaching.

`SwingEvidencePackage` is the hard boundary. Everything that chooses frames,
estimates stages, or estimates an impact interval belongs in that package.
Visual assessment, fault names, cue selection, and delivery consume the
package and are not fields on it.

Image pixels do not belong in any of these values. A frame is a reference:
camera, path, tick, timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_FAILED = "failed"
STATUSES = frozenset({STATUS_OK, STATUS_PARTIAL, STATUS_INSUFFICIENT, STATUS_FAILED})

CONFIDENCE_SCALE = "uncalibrated_heuristic"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Who produced a result. `created_at` is set when the result is written."""

    stage: str
    producer: str
    producer_version: str
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class FrameReference:
    """A stored frame. The bytes stay on disk."""

    camera_id: str
    relative_file: str
    capture_tick: int
    timestamp_seconds: float
    sha256: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class CameraCapture:
    camera_id: str
    frames: tuple[FrameReference, ...]


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Stage 1. Synchronized frames and metadata. No swing interpretation."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    capture_id: str | None
    cameras: tuple[CameraCapture, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SampledFrame:
    """One frame chosen by temporal sampling, still without a stage name."""

    reference: FrameReference
    order: int
    selection_reason: str


@dataclass(frozen=True, slots=True)
class TemporalSamplingResult:
    """Stage 2. Original-frame references in chronological order."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    camera_id: str
    frames: tuple[SampledFrame, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfidenceComponent:
    name: str
    value: float


@dataclass(frozen=True, slots=True)
class StageBoundary:
    """One estimated stage interval. Confidence is a heuristic, not a probability."""

    stage: str
    time_interval_seconds: tuple[float, float]
    confidence: float
    confidence_scale: str
    evidence_ticks: tuple[int, ...]
    method: str
    components: tuple[ConfidenceComponent, ...] = ()


@dataclass(frozen=True, slots=True)
class StageParameters:
    """The gates used for every shot. Recorded so a result can be reproduced."""

    visibility_min: float
    smooth_radius_seconds: float
    max_gap_seconds: float
    max_elevation_jump_torso: float
    min_torso_fraction: float
    min_rise_torso: float
    min_fall_torso: float
    peak_band_fraction: float
    min_follow_rise_torso: float
    accept_confidence: float
    min_run_samples: int
    setup_min_seconds: float
    setup_max_range_torso: float
    finish_min_seconds: float
    finish_max_range_torso: float
    finish_max_speed_torso_per_second: float


@dataclass(frozen=True, slots=True)
class MissingInterval:
    landmark: str
    time_interval_seconds: tuple[float, float]
    ticks: tuple[int, ...]
    frame_count: int


@dataclass(frozen=True, slots=True)
class LandmarkContinuity:
    landmark: str
    usable_frames: int
    longest_run: int
    continuity: float
    gaps: int


@dataclass(frozen=True, slots=True)
class TrackingSummary:
    frame_count: int
    pose_detected_frames: int
    pose_detection_rate: float
    usable_hand_frames: int
    both_wrists_frames: int
    continuity: tuple[LandmarkContinuity, ...]
    missing_landmark_intervals: tuple[MissingInterval, ...]


@dataclass(frozen=True, slots=True)
class HandExcursion:
    """Largest interior rise and fall of the hands. Not a stage and not impact."""

    observed: bool
    rise_torso_lengths: float | None
    fall_torso_lengths: float | None
    peak_time_seconds: float | None
    peak_tick: int | None
    wrists_at_peak: int | None
    gates_passed: bool
    reversal: bool


@dataclass(frozen=True, slots=True)
class ImpactObservation:
    """Ball contact is a separate question from hand motion."""

    supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SwingStageResult:
    """Stage 3. Temporal orientation only. No faults and no coaching."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    camera_id: str
    clock: str
    handedness: str
    orientation: str
    confidence_scale: str
    parameters: StageParameters
    boundaries: tuple[StageBoundary, ...]
    candidates: tuple[StageBoundary, ...]
    unresolved_stages: tuple[str, ...]
    tracking: TrackingSummary | None
    hand_excursion: HandExcursion
    impact: ImpactObservation
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssessmentFinding:
    """One visible strength or weakness. Evidence points at frames, not pixels."""

    kind: str
    fault_id: str | None
    confidence: float
    confidence_scale: str
    evidence_frames: tuple[FrameReference, ...]
    relied_on_stages: tuple[str, ...]
    note: str
    limitations: str = ""
    unverified: bool = False
    comparison: str | None = None


DESIRED_MOVEMENT_VISIBLE = "desired_movement_visible"
FAULT_STILL_VISIBLE = "fault_still_visible"
FOCUS_MIXED = "mixed"
FOCUS_NOT_EVALUABLE = "not_evaluable"
CURRENT_FOCUS_STATUSES = frozenset(
    {
        DESIRED_MOVEMENT_VISIBLE,
        FAULT_STILL_VISIBLE,
        FOCUS_MIXED,
        FOCUS_NOT_EVALUABLE,
    }
)

CHANGE_IMPROVED = "improved"
CHANGE_UNCHANGED = "unchanged"
CHANGE_REGRESSED = "regressed"
CHANGE_UNKNOWN = "unknown"
CHANGE_SINCE_PREVIOUS = frozenset(
    {CHANGE_IMPROVED, CHANGE_UNCHANGED, CHANGE_REGRESSED, CHANGE_UNKNOWN}
)

FOLLOW_UP_IMPROVED = "IMPROVED"
FOLLOW_UP_NOT_IMPROVED = "NOT_IMPROVED"
FOLLOW_UP_UNCERTAIN = "UNCERTAIN"
FOLLOW_UP_NOT_EVALUABLE = "NOT_EVALUABLE"
FOLLOW_UP_PROGRESS = frozenset(
    {
        FOLLOW_UP_IMPROVED,
        FOLLOW_UP_NOT_IMPROVED,
        FOLLOW_UP_UNCERTAIN,
        FOLLOW_UP_NOT_EVALUABLE,
    }
)

STAGE_CORRESPONDENCE_RELIABLE = "reliable"
STAGE_CORRESPONDENCE_UNCERTAIN = "uncertain"
STAGE_CORRESPONDENCE_UNAVAILABLE = "unavailable"
STAGE_CORRESPONDENCE = frozenset(
    {
        STAGE_CORRESPONDENCE_RELIABLE,
        STAGE_CORRESPONDENCE_UNCERTAIN,
        STAGE_CORRESPONDENCE_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class FocusFollowUp:
    """What the current frames say about the active focus.

    `current_focus_status` is a claim about this swing. `change_since_previous`
    is a claim that this swing differs from the previous one. A text note from
    the previous shot is not enough to support that second claim.
    """

    previous_focus_id: str
    current_focus_status: str
    change_since_previous: str
    progress_assessment: str
    current_focus_observation: str
    evidence_frames: tuple[FrameReference, ...] = ()
    limitations: str = ""
    previous_image_count: int = 0
    stage_correspondence: str = STAGE_CORRESPONDENCE_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class VisualAssessmentResult:
    """Stage 4. Findings only. It does not carry a replacement stage timeline."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    findings: tuple[AssessmentFinding, ...]
    no_proven_finding: bool
    stage_result_schema_version: str
    stage_uncertainties: tuple[str, ...]
    causal_hypotheses: tuple[AssessmentFinding, ...] = ()
    sequence: int | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    focus_follow_up: FocusFollowUp | None = None


@dataclass(frozen=True, slots=True)
class CoachingDecision:
    """Stage 5. Selects an approved cue id. It does not author cue text."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    outcome: str
    focus_fault_id: str | None
    cue_id: str | None
    assessment_schema_version: str
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


IMPACT_ESTIMATED = "estimated"
IMPACT_UNRESOLVED = "unresolved"
IMPACT_STATUSES = frozenset({IMPACT_ESTIMATED, IMPACT_UNRESOLVED})

OBSERVED_BALL_MOTION = "observed_ball_motion_onset"
INFERRED_CONTACT = "inferred_contact_interval"
UNSUPPORTED_ESTIMATE = "unsupported"


@dataclass(frozen=True, slots=True)
class RejectedImpactCandidate:
    """A blob that was considered and not accepted. Kept so a false start is visible."""

    normalized_x: float
    normalized_y: float
    reason: str
    onset_tick: int | None = None


@dataclass(frozen=True, slots=True)
class ImpactEstimate:
    """An interval around ball departure, or an explicit failure to see one.

    `classification` says what was actually observed. A ball leaving the tee
    is not a claim that the club was seen touching it. Confidence is the
    shared uncalibrated heuristic, not a probability.
    """

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    classification: str
    reason: str
    confidence: float | None
    confidence_scale: str
    method: str
    camera_id: str | None
    contact_interval_ticks: tuple[int, int] | None
    contact_interval_seconds: tuple[float, float] | None
    supporting_ticks: tuple[int, ...] = ()
    normalized_ball: tuple[float, float] | None = None
    rejected_candidates: tuple[RejectedImpactCandidate, ...] = ()
    camera_estimates: tuple[ImpactEstimate, ...] = ()
    parameters: tuple[tuple[str, float | int], ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceFrame:
    """One original frame chosen for a downstream model. The bytes stay on disk."""

    reference: FrameReference
    order: int
    selection_reason: str
    stage: str | None = None


@dataclass(frozen=True, slots=True)
class CameraClockComparison:
    """How far apart the two cameras are on the capture clock, by shared tick."""

    matched_ticks: int
    median_abs_delta_seconds: float | None
    max_abs_delta_seconds: float | None
    sufficient: bool
    note: str


@dataclass(frozen=True, slots=True)
class EvidenceAllocation:
    """How the image budget was split. This is not a coaching choice."""

    preset: str
    budget: int
    requested: tuple[tuple[str, int], ...]
    applied: tuple[tuple[str, int], ...]
    missing_cameras: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class SwingEvidencePackage:
    """Immutable contract between evidence production and any later assessor.

    No fault ids, cue ids, advice, or ball-flight numbers. Regenerating this
    from the same raw capture is allowed; replacing the raw frames is not.
    """

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    cameras: tuple[str, ...]
    image_budget: int
    selected_frames: tuple[EvidenceFrame, ...]
    stage_estimates: tuple[SwingStageResult, ...]
    impact_estimate: ImpactEstimate
    synchronization: CameraClockComparison
    fallback: str
    raw_capture_preserved: bool
    allocation: EvidenceAllocation | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoachingDeliveryResult:
    """Stage 6. Presents the decision it was given."""

    schema_version: str
    shot_id: str
    status: str
    provenance: Provenance
    decision_schema_version: str
    presented: bool
    channels: tuple[str, ...]
    suppression: str | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def to_plain(value: Any) -> Any:
    """JSON-ready form. Floats are rounded so a file is stable and readable."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_plain(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [to_plain(item) for item in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def require_header(payload: dict[str, Any]) -> None:
    """Every stage result carries the same identity fields."""

    missing = [
        key
        for key in ("schema_version", "shot_id", "status", "provenance")
        if key not in payload
    ]
    if missing:
        raise ValueError(f"result is missing {', '.join(missing)}")
    if not isinstance(payload["shot_id"], str) or not payload["shot_id"]:
        raise ValueError("shot_id must be a non-empty string")
    if payload["status"] not in STATUSES:
        raise ValueError(f"unknown status {payload['status']!r}")
    provenance = payload["provenance"]
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be an object")
    for key in ("stage", "producer", "producer_version"):
        if not provenance.get(key):
            raise ValueError(f"provenance.{key} is required")
