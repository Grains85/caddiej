"""Wire format for the floor state feed.

The browser is a rendering client: every message below flows server to client
except the small client control set at the bottom. Nothing here lets a client
assert state.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from caddiej.floor.state import (
    BallMarker,
    ClubPath,
    FlightDrawPoint,
    FloorAdvice,
    FloorFlourish,
    FloorPoint,
    FloorState,
    RoundResultView,
    ShotMetrics,
)

PROTOCOL_VERSION = 1


class FloorPointPayload(BaseModel):
    """A point in calibrated surface coordinates (0..1).

    These are render coordinates, not metres. Golf geometry has already been
    done: the browser draws the point it is given.
    """

    x: float
    y: float

    @classmethod
    def from_point(cls, point: FloorPoint) -> "FloorPointPayload":
        return cls(x=point.x, y=point.y)


class BallMarkerPayload(BaseModel):
    position: FloorPointPayload
    variant: str
    visible: bool

    @classmethod
    def from_marker(cls, marker: BallMarker) -> "BallMarkerPayload":
        return cls(
            position=FloorPointPayload.from_point(marker.position),
            variant=marker.variant.value,
            visible=marker.visible,
        )


class ShotMetricsPayload(BaseModel):
    ball_speed_mph: float
    launch_angle_degrees: float
    # Null when the device did not measure it. The overlay leaves the row out
    # rather than rendering a zero.
    carry_yards: float | None = None

    @classmethod
    def from_metrics(cls, metrics: ShotMetrics) -> "ShotMetricsPayload":
        return cls(
            ball_speed_mph=metrics.ball_speed_mph,
            launch_angle_degrees=metrics.launch_angle_degrees,
            carry_yards=metrics.carry_yards,
        )


class ClubPathPayload(BaseModel):
    """A club path as both endpoints plus the ray that produced them.

    `start` and `end` are calibrated surface points. `angle_degrees` is the
    physical launch-monitor angle and `length` is metres of display line —
    documentary, so a snapshot is readable on a projector that has no console.
    The renderer draws the two points and never does the trigonometry itself.
    """

    start: FloorPointPayload
    end: FloorPointPayload
    angle_degrees: float
    length: float

    @classmethod
    def from_club_path(cls, club_path: ClubPath) -> "ClubPathPayload":
        return cls(
            start=FloorPointPayload.from_point(club_path.start),
            end=FloorPointPayload.from_point(club_path.end),
            angle_degrees=club_path.angle_degrees,
            length=club_path.length,
        )


class AdvicePayload(BaseModel):
    """A coaching line, as the overlay receives it.

    Some of this text was written by a language model, so it crosses this
    boundary as data and is assigned to the DOM as text rather than as markup.
    The band travels instead of the severity: a renderer picks a colour, it does
    not judge a swing.
    """

    text: str
    band: str
    pending: bool

    @classmethod
    def from_advice(cls, advice: FloorAdvice) -> "AdvicePayload":
        return cls(text=advice.text, band=advice.band.value, pending=advice.pending)


class FlourishPayload(BaseModel):
    """A celebration in progress, as the overlay receives it.

    `seconds` is on the wire so the animation runs for as long as the effect is
    being held rather than for a length the stylesheet guessed. `cue` is the
    styling hook and is a fixed vocabulary from the catalogue, never free text —
    the renderer keys colour off it, so a value nobody chose deliberately would
    silently fall back to a default.
    """

    cue: str
    label: str
    intensity: float
    seconds: float

    @classmethod
    def from_flourish(cls, flourish: FloorFlourish) -> "FlourishPayload":
        return cls(
            cue=flourish.cue,
            label=flourish.label,
            intensity=flourish.intensity,
            seconds=flourish.seconds,
        )


class RoundResultPayload(BaseModel):
    """GSPro round-play numbers: raw values plus HUD display strings."""

    shot_id: str
    player_name: str = ""
    hole: int | None = None
    hole_shot: int | None = None
    ball_speed_raw: float | None = None
    total_distance_raw: float | None = None
    ball_speed_display: str | None = None
    total_distance_display: str | None = None
    starting_surface_raw: int | None = None
    ending_surface_raw: int | None = None

    @classmethod
    def from_result(cls, result: RoundResultView) -> "RoundResultPayload":
        return cls(
            shot_id=result.shot_id,
            player_name=result.player_name,
            hole=result.hole,
            hole_shot=result.hole_shot,
            ball_speed_raw=result.ball_speed_raw,
            total_distance_raw=result.total_distance_raw,
            ball_speed_display=result.ball_speed_display,
            total_distance_display=result.total_distance_display,
            starting_surface_raw=result.starting_surface_raw,
            ending_surface_raw=result.ending_surface_raw,
        )


class FlightDrawPointPayload(BaseModel):
    """A point in the diagnostic ball-flight chart (0..1 of that panel)."""

    x: float
    y: float

    @classmethod
    def from_point(cls, point: FlightDrawPoint) -> "FlightDrawPointPayload":
        return cls(x=point.x, y=point.y)


class FloorStatePayload(BaseModel):
    """Serialised floor state. Mirrors `FloorState` exactly, with no derivation."""

    version: int
    mode: str
    status_text: str
    marker: BallMarkerPayload
    metrics: ShotMetricsPayload | None
    club_path: ClubPathPayload | None
    advice: AdvicePayload | None = None
    flourish: FlourishPayload | None = None
    play_status: str = ""
    club_label: str = ""
    round_result: RoundResultPayload | None = None
    ball_flight: list[FlightDrawPointPayload] | None = None

    @classmethod
    def from_state(cls, state: FloorState) -> "FloorStatePayload":
        return cls(
            version=state.version,
            mode=state.mode.value,
            status_text=state.status_text,
            marker=BallMarkerPayload.from_marker(state.marker),
            metrics=(
                None
                if state.metrics is None
                else ShotMetricsPayload.from_metrics(state.metrics)
            ),
            club_path=(
                None
                if state.club_path is None
                else ClubPathPayload.from_club_path(state.club_path)
            ),
            advice=(
                None
                if state.advice is None
                else AdvicePayload.from_advice(state.advice)
            ),
            flourish=(
                None
                if state.flourish is None
                else FlourishPayload.from_flourish(state.flourish)
            ),
            play_status=state.play_status,
            club_label=state.club_label,
            round_result=(
                None
                if state.round_result is None
                else RoundResultPayload.from_result(state.round_result)
            ),
            ball_flight=(
                None
                if state.ball_flight is None
                else [FlightDrawPointPayload.from_point(p) for p in state.ball_flight]
            ),
        )


class HelloMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["hello"] = "hello"
    protocol_version: int = PROTOCOL_VERSION
    server_time: str


class SnapshotMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["snapshot"] = "snapshot"
    seq: int
    state: FloorStatePayload


class PongMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["pong"] = "pong"


class ErrorMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["error"] = "error"
    detail: str


class ClientMessage(BaseModel):
    """The only messages a client may send.

    Unknown fields are ignored so an older or newer client cannot break the
    server by sending extra keys.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    surface: str | None = None
    message: str | None = None


def hello_message() -> HelloMessage:
    return HelloMessage(server_time=datetime.now(UTC).isoformat())


def snapshot_message(state: FloorState) -> SnapshotMessage:
    """Build a full snapshot.

    Snapshots are always complete: there is no delta protocol, because the state
    is small and a full snapshot removes an entire class of divergence bug.
    """

    return SnapshotMessage(seq=state.version, state=FloorStatePayload.from_state(state))
