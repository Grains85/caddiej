"""What a stored shot is, and how it becomes a row and comes back.

Reading is a full reconstruction: a `StoredShot` carries a real `Shot`, with its
source, its measurements and its club, not a thinner reporting copy. That is a
deliberate cost. It means every column is exercised on the way back in, so a
column that is written wrong is caught by a round trip rather than discovered a
season later when somebody looks at last winter's rounds.

Column names match domain field names wherever the domain has one, so the SQL
below can be read against `domain/shot.py` without a translation table.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any, Literal, cast
from uuid import UUID

from caddiej.domain.coaching import (
    Advice,
    AdviceFailure,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
    SeveritySource,
)
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)

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


def stored_shot(row: Row) -> StoredShot:
    """Rebuild a shot from its row, measurements and provenance included."""

    return StoredShot(
        shot=Shot(
            shot_id=UUID(row["shot_id"]),
            source=ShotSource(
                kind=cast(
                    Literal["gspro_connect", "replay", "synthetic"],
                    row["source_kind"],
                ),
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
        ),
    )
