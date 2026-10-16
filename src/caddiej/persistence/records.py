"""What a stored shot is, and how it becomes a row and comes back.

Reading is a full reconstruction: a `StoredShot` carries a real `Shot`, with its
source, its measurements and its club, not a thinner reporting copy. That is a
deliberate cost. It means every column is exercised on the way back in, so a
column that is written wrong is caught by a round trip rather than discovered a
season later when somebody looks at last winter's rounds.

Column names match domain field names wherever the domain has one, so the SQL
below can be read against `domain/shot.py` without a translation table.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any, cast
from uuid import UUID, uuid4

from caddiej.domain.coaching import (
    Advice,
    AdviceFailure,
    AdviceKind,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
    SeveritySource,
)
from caddiej.domain.cues import ConfidenceSource, CueId, EvidenceGrade, PanelView
from caddiej.domain.shot import (
    SHOT_SOURCE_KINDS,
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
    ShotSourceKind,
)
from caddiej.domain.swing_checkpoints import CheckpointId, CoverageLevel, Deviation
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.swing_review import SwingReview
from caddiej.domain.swing_score import (
    CheckpointScore,
    MechanicalScore,
    PhaseScore,
    ScoreComponent,
    ScoreComponentId,
    SwingScore,
    VisualRating,
)
from caddiej.domain.vx import VX_METRICS_PROVENANCE, CoachResult

_BALL_COLUMNS = (
    "ball_speed_mph",
    "launch_angle_degrees",
    "launch_direction_degrees",
    "total_spin_rpm",
    "spin_axis_degrees",
    "back_spin_rpm",
    "side_spin_rpm",
    "carry_yards",
)

_CLUB_COLUMNS = (
    "club_speed_mph",
    "club_speed_at_impact_mph",
    "angle_of_attack_degrees",
    "club_path_degrees",
    "face_to_target_degrees",
    "loft_degrees",
    "lie_degrees",
    "closure_rate_deg_per_sec",
    "impact_horizontal_mm",
    "impact_vertical_mm",
)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One run of CaddieJ that recorded shots."""

    session_id: UUID
    started_at: datetime
    ended_at: datetime | None = None
    label: str | None = None
    app_version: str | None = None
    shot_count: int = 0


@dataclass(frozen=True, slots=True)
class RawPayload:
    """Exactly what a device said about one shot. Opaque, and disposable."""

    shot_id: UUID
    captured_at: datetime
    source_kind: str
    payload: str
    content_type: str = "application/x-ndjson"


@dataclass(frozen=True, slots=True)
class AnalysisReference:
    """A pointer to something produced about a shot by a later milestone.

    `reference` is opaque on purpose — a file path, a URL, an external id — so
    that adding swing video or AI analysis needs no schema change and nothing
    here has to understand what either of them is.
    """

    analysis_id: UUID
    shot_id: UUID
    kind: str
    reference: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredShot:
    """A shot, plus the things that are true of it only once it is stored."""

    shot: Shot
    session_id: UUID
    ordinal: int
    recorded_at: datetime
    player_name: str | None = None


@dataclass(frozen=True, slots=True)
class StoredAdvice:
    """An `AdviceOutcome` with the identity it acquired by being stored."""

    advice_id: UUID
    outcome: AdviceOutcome


def _utc(value: datetime) -> str:
    """Store an instant as UTC ISO-8601, always with an offset.

    A naive timestamp in a database is a timestamp nobody can interpret in a
    year, so the timezone is forced rather than assumed.
    """

    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def shot_row(stored: StoredShot) -> dict[str, Any]:
    """Flatten a shot into the columns of the `shots` table."""

    shot, source = stored.shot, stored.shot.source
    ball, club = shot.ball, shot.club_delivery
    return {
        "shot_id": str(shot.shot_id),
        "session_id": str(stored.session_id),
        "ordinal": stored.ordinal,
        "observed_at": _utc(source.observed_at),
        "recorded_at": _utc(stored.recorded_at),
        "player_name": stored.player_name,
        "handedness": shot.handedness.value,
        "club_kind": None if shot.club is None else shot.club.kind.value,
        "club_vendor_code": None if shot.club is None else shot.club.vendor_code,
        "ball_speed_mph": None if ball is None else ball.speed_mph,
        "launch_angle_degrees": None if ball is None else ball.launch_angle_degrees,
        "launch_direction_degrees": (
            None if ball is None else ball.launch_direction_degrees
        ),
        "total_spin_rpm": None if ball is None else ball.total_spin_rpm,
        "spin_axis_degrees": None if ball is None else ball.spin_axis_degrees,
        "back_spin_rpm": None if ball is None else ball.back_spin_rpm,
        "side_spin_rpm": None if ball is None else ball.side_spin_rpm,
        "carry_yards": None if ball is None else ball.carry_yards,
        "club_speed_mph": None if club is None else club.speed_mph,
        "club_speed_at_impact_mph": (
            None if club is None else club.speed_at_impact_mph
        ),
        "angle_of_attack_degrees": (
            None if club is None else club.angle_of_attack_degrees
        ),
        "club_path_degrees": None if club is None else club.path_degrees,
        "face_to_target_degrees": (
            None if club is None else club.face_to_target_degrees
        ),
        "loft_degrees": None if club is None else club.loft_degrees,
        "lie_degrees": None if club is None else club.lie_degrees,
        "closure_rate_deg_per_sec": (
            None if club is None else club.closure_rate_degrees_per_second
        ),
        "impact_horizontal_mm": None if club is None else club.impact_horizontal_mm,
        "impact_vertical_mm": None if club is None else club.impact_vertical_mm,
        "source_kind": source.kind,
        "source_device_id": source.device_id,
        "source_device_session_id": str(source.device_session_id),
        "source_device_sequence": source.device_sequence,
        "source_reported_units": source.reported_units,
        "source_api_version": source.api_version,
    }


def _source_kind(value: object) -> ShotSourceKind:
    """The provenance column, checked against what the domain actually allows.

    This used to be a cast at a hand-written list of three kinds, which had
    drifted: `gspro_round` — the only kind a passively observed session ever
    writes — was missing from it. A cast does nothing at runtime, so every
    observed shot was read back through a type that said it could not exist,
    and the round trip the schema tests rely on had never once carried a
    production shot. Checked against the one definition instead, and loud
    when a row says something the domain has never heard of.
    """

    if isinstance(value, str) and value in SHOT_SOURCE_KINDS:
        return cast(ShotSourceKind, value)
    raise ValueError(f"unknown shot source kind in history: {value!r}")


def stored_shot(row: Row) -> StoredShot:
    """Rebuild a shot from its row, measurements and provenance included."""

    return StoredShot(
        shot=Shot(
            shot_id=UUID(row["shot_id"]),
            source=ShotSource(
                kind=_source_kind(row["source_kind"]),
                device_session_id=UUID(row["source_device_session_id"]),
                observed_at=_instant(row["observed_at"]),
                # Only ever meaningful inside the process that measured it, so
                # it is not stored and reads back as the start of that process.
                observed_monotonic=0.0,
                device_id=row["source_device_id"],
                device_sequence=row["source_device_sequence"],
                reported_units=row["source_reported_units"],
                api_version=row["source_api_version"],
            ),
            ball=_ball(row),
            club_delivery=_club_delivery(row),
            handedness=Handedness(row["handedness"]),
            club=_club(row),
        ),
        session_id=UUID(row["session_id"]),
        ordinal=row["ordinal"],
        recorded_at=_instant(row["recorded_at"]),
        player_name=row["player_name"],
    )


def _ball(row: Row) -> BallLaunch | None:
    if all(row[column] is None for column in _BALL_COLUMNS):
        return None
    return BallLaunch(
        speed_mph=row["ball_speed_mph"],
        launch_angle_degrees=row["launch_angle_degrees"],
        launch_direction_degrees=row["launch_direction_degrees"],
        total_spin_rpm=row["total_spin_rpm"],
        spin_axis_degrees=row["spin_axis_degrees"],
        back_spin_rpm=row["back_spin_rpm"],
        side_spin_rpm=row["side_spin_rpm"],
        carry_yards=row["carry_yards"],
    )


def _club_delivery(row: Row) -> ClubDelivery | None:
    if all(row[column] is None for column in _CLUB_COLUMNS):
        return None
    return ClubDelivery(
        speed_mph=row["club_speed_mph"],
        speed_at_impact_mph=row["club_speed_at_impact_mph"],
        angle_of_attack_degrees=row["angle_of_attack_degrees"],
        path_degrees=row["club_path_degrees"],
        face_to_target_degrees=row["face_to_target_degrees"],
        loft_degrees=row["loft_degrees"],
        lie_degrees=row["lie_degrees"],
        closure_rate_degrees_per_second=row["closure_rate_deg_per_sec"],
        impact_horizontal_mm=row["impact_horizontal_mm"],
        impact_vertical_mm=row["impact_vertical_mm"],
    )


def _club(row: Row) -> Club | None:
    if row["club_kind"] is None:
        return None
    return Club(kind=ClubKind(row["club_kind"]), vendor_code=row["club_vendor_code"])


def session_row(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": str(record.session_id),
        "started_at": _utc(record.started_at),
        "ended_at": None if record.ended_at is None else _utc(record.ended_at),
        "label": record.label,
        "app_version": record.app_version,
        "shot_count": record.shot_count,
    }


def session_record(row: Row) -> SessionRecord:
    return SessionRecord(
        session_id=UUID(row["session_id"]),
        started_at=_instant(row["started_at"]),
        ended_at=None if row["ended_at"] is None else _instant(row["ended_at"]),
        label=row["label"],
        app_version=row["app_version"],
        shot_count=row["shot_count"],
    )


def payload_row(payload: RawPayload) -> dict[str, Any]:
    return {
        "shot_id": str(payload.shot_id),
        "captured_at": _utc(payload.captured_at),
        "source_kind": payload.source_kind,
        "content_type": payload.content_type,
        "payload": payload.payload,
    }


def raw_payload(row: Row) -> RawPayload:
    return RawPayload(
        shot_id=UUID(row["shot_id"]),
        captured_at=_instant(row["captured_at"]),
        source_kind=row["source_kind"],
        content_type=row["content_type"],
        payload=row["payload"],
    )


def analysis_row(reference: AnalysisReference) -> dict[str, Any]:
    return {
        "analysis_id": str(reference.analysis_id),
        "shot_id": str(reference.shot_id),
        "kind": reference.kind,
        "reference": reference.reference,
        "created_at": _utc(reference.created_at),
    }


def analysis_reference(row: Row) -> AnalysisReference:
    return AnalysisReference(
        analysis_id=UUID(row["analysis_id"]),
        shot_id=UUID(row["shot_id"]),
        kind=row["kind"],
        reference=row["reference"],
        created_at=_instant(row["created_at"]),
    )


def advice_row(stored: StoredAdvice) -> dict[str, Any]:
    """Flatten a coaching outcome into the columns of `shot_advice`."""

    outcome = stored.outcome
    advice = outcome.advice
    return {
        "advice_id": str(stored.advice_id),
        "shot_id": str(advice.shot_id),
        "created_at": _utc(advice.created_at),
        "status": outcome.status.value,
        "focus": advice.focus.value,
        "severity": advice.severity,
        "severity_source": advice.severity_source.value,
        "source": advice.source.value,
        "baseline_shots": outcome.baseline_shots,
        # Empty means "nothing was said", which is a real outcome for a good
        # shot, so it is stored as null rather than as an empty string.
        "display": advice.display or None,
        "speech": advice.speech or None,
        "provider": outcome.provider,
        "model": outcome.model,
        "prompt_version": outcome.prompt_version,
        "latency_ms": outcome.latency_ms,
        "input_images": outcome.input_images,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cache_hit": int(outcome.cache_hit),
        "fingerprint": outcome.fingerprint,
        "failure_reason": None if outcome.failure is None else outcome.failure.value,
        "detail": outcome.detail,
        "clip_path": outcome.clip_path,
        "cue_id": None if advice.cue is None else advice.cue.value,
        "confidence": advice.confidence,
        "confidence_source": (
            None if advice.confidence_source is None else advice.confidence_source.value
        ),
        "evidence_grades": outcome.evidence_grades,
        "capture_id": outcome.capture_id,
        "panels_used": outcome.panels_used,
        "kind": advice.kind.value,
    }


def stored_advice(row: Row) -> StoredAdvice:
    """Rebuild a coaching outcome from its row."""

    return StoredAdvice(
        advice_id=UUID(row["advice_id"]),
        outcome=AdviceOutcome(
            advice=Advice(
                shot_id=UUID(row["shot_id"]),
                focus=CoachingFocus(row["focus"]),
                display=row["display"] or "",
                speech=row["speech"] or "",
                severity=row["severity"],
                created_at=_instant(row["created_at"]),
                severity_source=SeveritySource(row["severity_source"]),
                source=AdviceSource(row["source"]),
                kind=_advice_kind(row),
                cue=_cue_id(row),
                confidence=row["confidence"] if "confidence" in row.keys() else None,
                confidence_source=_confidence_source(row),
            ),
            status=AdviceStatus(row["status"]),
            baseline_shots=row["baseline_shots"],
            provider=row["provider"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            latency_ms=row["latency_ms"],
            input_images=row["input_images"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_hit=bool(row["cache_hit"]),
            failure=(
                None
                if row["failure_reason"] is None
                else AdviceFailure(row["failure_reason"])
            ),
            detail=row["detail"],
            clip_path=row["clip_path"],
            fingerprint=row["fingerprint"],
            capture_id=row["capture_id"] if "capture_id" in row.keys() else None,
            panels_used=row["panels_used"] if "panels_used" in row.keys() else None,
            evidence_grades=(
                row["evidence_grades"] if "evidence_grades" in row.keys() else None
            ),
        ),
    )


def _cue_id(row: Row) -> CueId | None:
    if "cue_id" not in row.keys() or not row["cue_id"]:
        return None
    try:
        return CueId(row["cue_id"])
    except ValueError:
        return None


def _confidence_source(row: Row) -> ConfidenceSource | None:
    if "confidence_source" not in row.keys() or not row["confidence_source"]:
        return None
    try:
        return ConfidenceSource(row["confidence_source"])
    except ValueError:
        return None


def _advice_kind(row: Row) -> AdviceKind:
    if "kind" not in row.keys() or not row["kind"]:
        return AdviceKind.CORRECTION
    try:
        return AdviceKind(row["kind"])
    except ValueError:
        return AdviceKind.CORRECTION


def coach_result_row(session_id: UUID, result: CoachResult) -> dict[str, object]:
    """A ProTee-only coaching outcome as a `coach_results` row.

    Metrics are stored whole, nulls included, as JSON: every reading keeps its
    unit and ProTee's direction word, so nothing about how it was written is
    lost to a column type.
    """

    metrics = result.metrics
    return {
        "result_id": str(uuid4()),
        "session_id": str(session_id),
        "strike_id": str(result.strike_id),
        "trigger_id": result.trigger_id,
        "capture_id": None if result.capture_id is None else str(result.capture_id),
        "created_at": result.created_at.isoformat(),
        "status": result.status,
        "kind": result.kind,
        "focus": result.focus,
        "cue_id": result.cue_id,
        "display": result.display,
        "speech": result.speech,
        "severity": result.severity,
        "confidence": result.confidence,
        "confidence_source": result.confidence_source,
        "provider": result.provider,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
        "input_images": result.input_images,
        "swing_images": result.swing_images,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "metrics_source": result.metrics_source,
        "metrics_status": result.metrics_status,
        "metrics_provenance": None if metrics is None else VX_METRICS_PROVENANCE,
        "metrics_read": 0 if metrics is None else len(metrics.available),
        "metrics_json": (
            None if metrics is None else json.dumps(metrics.as_dict(), sort_keys=False)
        ),
        "metrics_image": result.metrics_image,
        "metrics_captured_at": (
            None
            if result.metrics_captured_at is None
            else result.metrics_captured_at.isoformat()
        ),
        "panels_used": result.panels_used,
        "evidence_grades": result.evidence_grades,
        "failure_reason": result.failure_reason,
        "detail": result.detail,
        **swing_score_columns(
            result.swing_score, review=result.review, final_cue=result.cue_id
        ),
    }


@dataclass(frozen=True, slots=True)
class StoredSwingScore:
    """One strike's score as history holds it, for drawing a session trend."""

    strike_id: UUID
    created_at: datetime
    score: SwingScore


def swing_score_columns(
    score: SwingScore | None,
    *,
    review: SwingReview | None = None,
    final_cue: str | None = None,
) -> dict[str, object]:
    """The four `coach_results` columns a score occupies.

    The JSON is written in a fixed key order with Python's shortest round-trip
    float repr, so the same score always produces the same text and reading it
    back produces the same floats.

    The rubric version is in the document as well as in its own column, and
    reading never assumes the current one. A `swing-score-v1` row is a
    delivery-only score of five components and it stays one: the v2 keys it
    has no values for read back absent rather than as zeros, which is the same
    rule the scoring itself follows. Rescoring last season's shots under this
    season's rubric would make a session trend a graph of CaddieJ's edits.
    """

    if score is None:
        return {
            "swing_score": None,
            "swing_score_rubric": None,
            "swing_score_club": None,
            "swing_score_json": None,
        }
    document: dict[str, object] = {
        "rubric_version": score.rubric_version,
        "value": score.value,
        "available_weight": score.available_weight,
        "delivery_value": score.delivery_value,
        "visual_value": score.visual_value,
        "ceiling": score.ceiling,
        "club": None if score.club is None else score.club.value,
        "components": [
            {
                "component": item.component.value,
                "weight": item.weight,
                "grade": item.grade.value,
                "metrics": list(item.metrics),
                "value": item.value,
                "sub_score": item.sub_score,
                "rating": None if item.rating is None else item.rating.value,
                "frame_id": item.frame_id,
                "confidence": item.confidence,
            }
            for item in score.components
        ],
    }
    if score.mechanics is not None:
        document["mechanics"] = _mechanics_document(score.mechanics)
    if review is not None:
        document["review"] = review_document(review, final_cue)
    return {
        "swing_score": score.value,
        "swing_score_rubric": score.rubric_version,
        "swing_score_club": None if score.club is None else score.club.value,
        "swing_score_json": json.dumps(document),
    }


def swing_score_of(text: str) -> SwingScore:
    """Rebuild a score from its JSON. Raises on a document it did not write."""

    document = json.loads(text)
    club = document["club"]
    mechanics, legacy = _mechanics_of(document.get("mechanics"))
    return SwingScore(
        value=document["value"],
        components=tuple(
            ScoreComponent(
                component=ScoreComponentId(item["component"]),
                weight=int(item["weight"]),
                grade=EvidenceGrade(item["grade"]),
                metrics=tuple(str(name) for name in item["metrics"]),
                value=item["value"],
                sub_score=item["sub_score"],
                rating=_visual_rating(item.get("rating")),
                frame_id=item.get("frame_id"),
                confidence=item.get("confidence"),
            )
            for item in document["components"]
        ),
        available_weight=int(document["available_weight"]),
        club=None if club is None else ClubKind(club),
        rubric_version=str(document["rubric_version"]),
        delivery_value=document.get("delivery_value"),
        visual_value=document.get("visual_value"),
        ceiling=document.get("ceiling"),
        mechanics=mechanics,
        legacy_mechanics_json=legacy,
    )


def _mechanics_document(mechanics: MechanicalScore) -> dict[str, object]:
    """Swing Score V4, written out whole.

    Every checkpoint in the catalogue, assessed or not, with its whole
    provenance: score, confidence, picture, camera, the phase the model said
    the picture shows, its factual note, the direction of the miss, and the
    score it asked for if that was clamped. The phase roll-up and the ceiling
    that bound are written too, so a stored score can be read without
    re-implementing the rubric.
    """

    return {
        "value": mechanics.value,
        "raw_value": mechanics.raw_value,
        "coverage": mechanics.coverage,
        "coverage_level": mechanics.coverage_level.value,
        "confidence": mechanics.confidence,
        "ceiling": mechanics.ceiling,
        "ceiling_reasons": list(mechanics.ceiling_reasons),
        "capped": [item.value for item in mechanics.capped],
        "checkpoints": [
            {
                "checkpoint": item.checkpoint.value,
                "phase": item.phase.value,
                "weight": item.weight,
                "scoreable": item.scoreable,
                "score": item.score,
                "confidence": item.confidence,
                "frame_id": item.frame_id,
                "seen_phase": None
                if item.seen_phase is None
                else item.seen_phase.value,
                "view": item.view.value,
                "observation": item.observation,
                "deviation": None if item.deviation is None else item.deviation.value,
                "capped_from": item.capped_from,
            }
            for item in mechanics.checkpoints
        ],
        "phases": [
            {
                "phase": item.phase.value,
                "weight": item.weight,
                "value": item.value,
                "coverage": item.coverage,
                "confidence": item.confidence,
                "assessed": item.assessed,
                "scoreable": item.scoreable,
                "mean": item.mean,
                "worst": None if item.worst is None else item.worst.value,
                "level": item.level.value,
            }
            for item in mechanics.phases
        ],
    }


def _mechanics_of(raw: object) -> tuple[MechanicalScore | None, str | None]:
    """The v4 mechanics, or a v3 block kept verbatim, or nothing."""

    if not isinstance(raw, dict):
        return None, None
    if "checkpoints" not in raw:
        return None, json.dumps(raw)
    return (
        MechanicalScore(
            checkpoints=tuple(
                CheckpointScore(
                    checkpoint=CheckpointId(item["checkpoint"]),
                    phase=SwingPhase(item["phase"]),
                    weight=int(item["weight"]),
                    scoreable=bool(item["scoreable"]),
                    score=item.get("score"),
                    confidence=item.get("confidence"),
                    frame_id=item.get("frame_id"),
                    seen_phase=_phase_or_none(item.get("seen_phase")),
                    view=PanelView(item.get("view", PanelView.NONE.value)),
                    observation=str(item.get("observation") or ""),
                    deviation=_deviation_or_none(item.get("deviation")),
                    capped_from=item.get("capped_from"),
                )
                for item in raw["checkpoints"]
            ),
            phases=tuple(
                PhaseScore(
                    phase=SwingPhase(item["phase"]),
                    weight=int(item["weight"]),
                    value=item.get("value"),
                    coverage=float(item.get("coverage", 0.0)),
                    confidence=float(item.get("confidence", 0.0)),
                    assessed=int(item.get("assessed", 0)),
                    scoreable=int(item.get("scoreable", 0)),
                    mean=item.get("mean"),
                    worst=_checkpoint_or_none(item.get("worst")),
                    level=CoverageLevel(
                        item.get("level", CoverageLevel.INSUFFICIENT.value)
                    ),
                )
                for item in raw["phases"]
            ),
            value=raw["value"],
            coverage=float(raw["coverage"]),
            coverage_level=CoverageLevel(raw["coverage_level"]),
            confidence=float(raw["confidence"]),
            raw_value=raw.get("raw_value"),
            ceiling=raw.get("ceiling"),
            ceiling_reasons=tuple(str(item) for item in raw.get("ceiling_reasons", ())),
            capped=tuple(CheckpointId(item) for item in raw.get("capped", ())),
        ),
        None,
    )


def review_document(
    review: SwingReview, final_cue: str | None = None
) -> dict[str, object]:
    """Faults, root-cause ranking, session lesson and deltas, as JSON."""

    root = review.root
    progress = review.progress
    return {
        "source": review.source.value,
        "final_cue": final_cue,
        "previous_score": review.previous_score,
        "protee": [item.value for item in review.protee],
        "faults": [
            {
                "fault": item.fault.value,
                "phase": item.phase.value,
                "severity": item.severity,
                "confidence": item.confidence,
                "seen_confidence": item.seen_confidence,
                "triggers": [name.value for name in item.triggers],
                "supporting": [name.value for name in item.supporting],
                "worst_score": item.worst_score,
                "frame_id": item.frame_id,
                "corroborated_by": [sign.value for sign in item.corroborated_by],
            }
            for item in review.faults
        ],
        "root": None
        if root is None
        else {
            "fault": None if root.fault is None else root.fault.value,
            "cue": None if root.cue is None else root.cue.value,
            "reason": root.reason,
            "explains": [item.value for item in root.explains],
            "sticky": root.sticky,
            "frame_id": root.frame_id,
            "confidence": root.confidence,
            "severity": root.severity,
            "ranking": [
                {
                    "fault": row.fault.value,
                    "priority": row.priority,
                    "effective": row.effective,
                    "explained_by": None
                    if row.explained_by is None
                    else row.explained_by.value,
                    "explains": [item.value for item in row.explains],
                    "coachable": row.coachable,
                }
                for row in root.ranking
            ],
        },
        "lesson": None
        if progress is None
        else {
            "fault": progress.fault.value,
            "cue": progress.cue.value,
            "phase": progress.phase.value,
            "checkpoints": [item.value for item in progress.checkpoints],
            "verdict": progress.verdict.value,
            "mode": progress.mode.value,
            "score": progress.score,
            "baseline": progress.baseline,
            "previous": progress.previous,
            "delta": progress.delta,
            "shots": progress.shots,
            "improved_shots": progress.improved_shots,
            "solved_shots": progress.solved_shots,
            "headline": progress.headline,
            "next_fault": None
            if progress.next_fault is None
            else progress.next_fault.value,
        },
        "deltas": [
            {
                "checkpoint": item.checkpoint.value,
                "previous": item.previous,
                "current": item.current,
                "delta": item.delta,
            }
            for item in review.deltas
        ],
    }


def review_of(text: str | None) -> dict[str, Any] | None:
    """The stored review document, as written, or None on a row without one."""

    if not text:
        return None
    document = json.loads(text)
    review = document.get("review") if isinstance(document, dict) else None
    return review if isinstance(review, dict) else None


def _phase_or_none(value: object) -> SwingPhase | None:
    return None if value is None else SwingPhase(str(value))


def _deviation_or_none(value: object) -> Deviation | None:
    return None if value is None else Deviation(str(value))


def _checkpoint_or_none(value: object) -> CheckpointId | None:
    return None if value is None else CheckpointId(str(value))


def _visual_rating(value: object) -> VisualRating | None:
    return None if value is None else VisualRating(str(value))


def stored_swing_score(row: Row) -> StoredSwingScore:
    return StoredSwingScore(
        strike_id=UUID(row["strike_id"]),
        created_at=_instant(row["created_at"]),
        score=swing_score_of(row["swing_score_json"]),
    )
