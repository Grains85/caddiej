"""Turning GSPro messages into CaddieJ facts.

This is the only module that imports both vocabularies, and it translates
without interpreting. It decides what a number means in CaddieJ's units; it
never decides what a shot means for the round.
"""

import logging
from dataclasses import dataclass

from caddiej.adapters.gspro.wire import (
    WireBallData,
    WireClubData,
    WireLaunchMonitorMessage,
    WireSimulatorResponse,
)
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Detection,
    Handedness,
)

logger = logging.getLogger(__name__)

_YARDS_PER_METRE = 1.0936132983377078

# GSPro reports distances in the unit the device declared. Speeds are mph and
# angles degrees regardless, which already matches CaddieJ's convention.
_METRIC_UNITS = frozenset({"meters", "metres", "meter", "metre"})

_WEDGE_CODES = frozenset({"PW", "GW", "AW", "SW", "LW"})


@dataclass(frozen=True, slots=True)
class MonitorStatus:
    """The device's own report about itself, as flags rather than golf state."""

    ready: bool | None
    ball: Detection


def carry_to_yards(value: float | None, units: str | None) -> float | None:
    """Convert a reported carry distance into yards."""

    if value is None:
        return None
    if units is not None and units.strip().lower() in _METRIC_UNITS:
        return value * _YARDS_PER_METRE
    return value


def to_ball_launch(data: WireBallData, units: str | None) -> BallLaunch:
    return BallLaunch(
        speed_mph=data.speed,
        launch_angle_degrees=data.vla,
        launch_direction_degrees=data.hla,
        total_spin_rpm=data.total_spin,
        spin_axis_degrees=data.spin_axis,
        back_spin_rpm=data.back_spin,
        side_spin_rpm=data.side_spin,
        carry_yards=carry_to_yards(data.carry_distance, units),
    )


def to_club_delivery(data: WireClubData) -> ClubDelivery:
    return ClubDelivery(
        speed_mph=data.speed,
        speed_at_impact_mph=data.speed_at_impact,
        angle_of_attack_degrees=data.angle_of_attack,
        path_degrees=data.path,
        face_to_target_degrees=data.face_to_target,
        loft_degrees=data.loft,
        lie_degrees=data.lie,
        closure_rate_degrees_per_second=data.closure_rate,
        impact_horizontal_mm=data.horizontal_face_impact,
        impact_vertical_mm=data.vertical_face_impact,
    )


def to_monitor_status(message: WireLaunchMonitorMessage) -> MonitorStatus:
    options = message.shot_data_options
    if options is None:
        return MonitorStatus(ready=None, ball=Detection.UNKNOWN)

    detected = options.launch_monitor_ball_detected
    if detected is None:
        ball = Detection.UNKNOWN
    else:
        ball = Detection.PRESENT if detected else Detection.ABSENT
    return MonitorStatus(ready=options.launch_monitor_is_ready, ball=ball)


def to_handedness(value: str | None) -> Handedness:
    match (value or "").strip().upper():
        case "RH" | "RIGHT":
            return Handedness.RIGHT
        case "LH" | "LEFT":
            return Handedness.LEFT
        case _:
            return Handedness.UNKNOWN


def to_club(code: str | None) -> Club | None:
    """Map a GSPro club code onto a CaddieJ club.

    An unrecognised code is a club CaddieJ does not categorise, not an error.
    The raw code is kept so the gap is visible in logs and history.
    """

    if code is None:
        return None
    normalised = code.strip().upper()
    if not normalised:
        return None
    return Club(kind=_club_kind(normalised), vendor_code=normalised)


def _club_kind(code: str) -> ClubKind:
    if code in ("DR", "D"):
        return ClubKind.DRIVER
    if code in ("PT", "P"):
        return ClubKind.PUTTER
    if code in _WEDGE_CODES:
        return ClubKind.WEDGE
    prefix, rest = code[0], code[1:]
    if rest.isdigit():
        match prefix:
            case "W":
                return ClubKind.WOOD
            case "H":
                return ClubKind.HYBRID
            case "I":
                return ClubKind.IRON
    logger.debug("gspro_unknown_club_code", extra={"gspro_club_code": code})
    return ClubKind.UNKNOWN


def player_from_response(
    response: WireSimulatorResponse,
) -> tuple[Handedness | None, Club | None]:
    """Extract handedness and club from a player-information response.

    Either may be absent; absence means "unchanged", not "unknown", so it is
    reported as None for the caller to ignore.
    """

    if response.player is None:
        return None, None
    player = response.player
    handedness = None if player.handed is None else to_handedness(player.handed)
    return handedness, to_club(player.club)
