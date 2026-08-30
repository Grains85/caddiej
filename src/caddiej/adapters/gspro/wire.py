"""The GSPro Open Connect v1 wire format.

This module is the only place in CaddieJ where GSPro's vocabulary exists:
`BallData`, `HLA`, `APIversion`, response code 201. Nothing outside the GSPro
adapter may import it, and a test enforces that.

Two decisions shape these models:

- Unknown fields are ignored, so a future GSPro release adding a field cannot
  stop shots being ingested.
- Fields the specification calls required are accepted as missing where a real
  device plausibly omits them, because rejecting a genuine shot is worse than
  ingesting a partial one. What is genuinely load-bearing — ball speed and the
  two launch angles — stays required.
"""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caddiej.adapters.gspro.errors import GSProProtocolError
from caddiej.domain.events import RejectionReason

# Rejecting NaN and infinity here keeps them out of the domain, out of the
# event log, and out of the JSON the overlay has to parse.
_WIRE = ConfigDict(populate_by_name=True, extra="ignore", allow_inf_nan=False)


class WireBallData(BaseModel):
    model_config = _WIRE

    speed: float = Field(alias="Speed")
    hla: float = Field(alias="HLA")
    vla: float = Field(alias="VLA")
    spin_axis: float | None = Field(default=None, alias="SpinAxis")
    total_spin: float | None = Field(default=None, alias="TotalSpin")
    back_spin: float | None = Field(default=None, alias="BackSpin")
    side_spin: float | None = Field(default=None, alias="SideSpin")
    carry_distance: float | None = Field(default=None, alias="CarryDistance")


class WireClubData(BaseModel):
    model_config = _WIRE

    speed: float | None = Field(default=None, alias="Speed")
    angle_of_attack: float | None = Field(default=None, alias="AngleOfAttack")
    face_to_target: float | None = Field(default=None, alias="FaceToTarget")
    lie: float | None = Field(default=None, alias="Lie")
    loft: float | None = Field(default=None, alias="Loft")
    path: float | None = Field(default=None, alias="Path")
    speed_at_impact: float | None = Field(default=None, alias="SpeedAtImpact")
    vertical_face_impact: float | None = Field(default=None, alias="VerticalFaceImpact")
    horizontal_face_impact: float | None = Field(
        default=None, alias="HorizontalFaceImpact"
    )
    closure_rate: float | None = Field(default=None, alias="ClosureRate")


class WireShotDataOptions(BaseModel):
    """What the device says its message contains, and how it is doing.

    Optional as a whole: a device that omits it should fall back to inferring
    content from which data blocks are present, rather than losing the shot.
    """

    model_config = _WIRE

    contains_ball_data: bool | None = Field(default=None, alias="ContainsBallData")
    contains_club_data: bool | None = Field(default=None, alias="ContainsClubData")
    launch_monitor_is_ready: bool | None = Field(
        default=None, alias="LaunchMonitorIsReady"
    )
    launch_monitor_ball_detected: bool | None = Field(
        default=None, alias="LaunchMonitorBallDetected"
    )
    is_heart_beat: bool | None = Field(default=None, alias="IsHeartBeat")


class WireLaunchMonitorMessage(BaseModel):
    """A message travelling from the launch monitor towards the simulator."""

    model_config = _WIRE

    device_id: str | None = Field(default=None, alias="DeviceID")
    units: str | None = Field(default=None, alias="Units")
    shot_number: int | None = Field(default=None, alias="ShotNumber")
    api_version: str | None = Field(default=None, alias="APIversion")
    ball_data: WireBallData | None = Field(default=None, alias="BallData")
    club_data: WireClubData | None = Field(default=None, alias="ClubData")
    shot_data_options: WireShotDataOptions | None = Field(
        default=None, alias="ShotDataOptions"
    )

    @property
    def is_heartbeat(self) -> bool:
        options = self.shot_data_options
        return options is not None and bool(options.is_heart_beat)

    @property
    def declares_ball_data(self) -> bool:
        """Whether ball data should be believed.

        The flag wins over the block's presence, because devices routinely send
        a zero-filled `BallData` on every heartbeat with `ContainsBallData`
        false. Trusting presence alone would turn each heartbeat into a shot
        travelling at zero miles per hour. When no flag is given, the data is
        all there is to go on, so it is trusted.
        """

        if self.ball_data is None:
            return False
        options = self.shot_data_options
        if options is None or options.contains_ball_data is None:
            return True
        return options.contains_ball_data

    @property
    def declares_club_data(self) -> bool:
        if self.club_data is None:
            return False
        options = self.shot_data_options
        if options is None or options.contains_club_data is None:
            return True
        return options.contains_club_data


class WirePlayer(BaseModel):
    model_config = _WIRE

    handed: str | None = Field(default=None, alias="Handed")
    club: str | None = Field(default=None, alias="Club")


class WireSimulatorResponse(BaseModel):
    """A message travelling from the simulator back to the launch monitor."""

    model_config = _WIRE

    code: int = Field(alias="Code")
    message: str | None = Field(default=None, alias="Message")
    player: WirePlayer | None = Field(default=None, alias="Player")


def _decode(raw: bytes) -> Any:
    # Invalid UTF-8 is replaced rather than raised: a mangled message becomes a
    # rejected message, not a dead connection.
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError as error:
        raise GSProProtocolError(
            RejectionReason.UNPARSEABLE, f"not valid JSON: {error}"
        ) from error


def _validate[ModelType: BaseModel](model: type[ModelType], payload: Any) -> ModelType:
    if not isinstance(payload, dict):
        raise GSProProtocolError(
            RejectionReason.SCHEMA_INVALID,
            f"expected a JSON object, got {type(payload).__name__}",
        )
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise GSProProtocolError(
            RejectionReason.SCHEMA_INVALID, _summarise(error)
        ) from error


def _summarise(error: ValidationError) -> str:
    """A short, log-safe description of why validation failed."""

    parts = [
        f"{'.'.join(str(item) for item in problem['loc'])}: {problem['msg']}"
        for problem in error.errors()[:3]
    ]
    return "; ".join(parts)


def parse_launch_monitor_message(raw: bytes) -> WireLaunchMonitorMessage:
    return _validate(WireLaunchMonitorMessage, _decode(raw))


def parse_simulator_response(raw: bytes) -> WireSimulatorResponse:
    return _validate(WireSimulatorResponse, _decode(raw))
