"""Normalized shot, club, and player facts.

These models are CaddieJ's own vocabulary, not any launch monitor's. A vendor
payload is translated into these at the adapter boundary and never travels
further, which is what allows a second launch monitor to be added later without
touching anything that consumes a shot.

Units are fixed and named in every field: speeds in mph, angles in degrees,
spin in rpm, distances in yards. That convention matches the OpenShot open
schema, so exporting to it later is a mapping rather than a redesign. CaddieJ
takes no dependency on that service; only the vocabulary is shared.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID


class Handedness(StrEnum):
    RIGHT = "right"
    LEFT = "left"
    UNKNOWN = "unknown"


class ClubKind(StrEnum):
    """Coarse club category. Enough for behaviour, not a bag inventory."""

    DRIVER = "driver"
    WOOD = "wood"
    HYBRID = "hybrid"
    IRON = "iron"
    WEDGE = "wedge"
    PUTTER = "putter"
    UNKNOWN = "unknown"


class Detection(StrEnum):
    """Tri-state, because "the device never told us" is not "there is no ball".

    A launch monitor that does not report ball detection must not read as
    reporting an absent ball, or the floor would clear its marker forever.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def _require_finite(owner: str, **values: float | None) -> None:
    """Reject NaN and infinity.

    They cannot be represented in JSON, so they would reach the overlay as
    unparseable text, and they poison every average computed from them later.
    """

    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{owner}.{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class Club:
    """A club as CaddieJ understands it, plus the code the source used.

    The vendor code is kept for provenance and debugging. Nothing outside the
    adapter that produced it may branch on it: that is what `kind` is for.
    """

    kind: ClubKind
    vendor_code: str | None = None


@dataclass(frozen=True, slots=True)
class BallLaunch:
    """What the ball did. The part of a shot every launch monitor measures."""

    speed_mph: float
    launch_angle_degrees: float
    launch_direction_degrees: float
    total_spin_rpm: float | None = None
    spin_axis_degrees: float | None = None
    back_spin_rpm: float | None = None
    side_spin_rpm: float | None = None
    carry_yards: float | None = None

    def __post_init__(self) -> None:
        _require_finite(
            "BallLaunch",
            speed_mph=self.speed_mph,
            launch_angle_degrees=self.launch_angle_degrees,
            launch_direction_degrees=self.launch_direction_degrees,
            total_spin_rpm=self.total_spin_rpm,
            spin_axis_degrees=self.spin_axis_degrees,
            back_spin_rpm=self.back_spin_rpm,
            side_spin_rpm=self.side_spin_rpm,
            carry_yards=self.carry_yards,
        )
        if self.speed_mph < 0:
            raise ValueError("BallLaunch.speed_mph must not be negative")


@dataclass(frozen=True, slots=True)
class ClubDelivery:
    """How the club arrived. Optional: many devices report no club data at all."""

    speed_mph: float | None = None
    speed_at_impact_mph: float | None = None
    angle_of_attack_degrees: float | None = None
    path_degrees: float | None = None
    face_to_target_degrees: float | None = None
    loft_degrees: float | None = None
    lie_degrees: float | None = None
    closure_rate_degrees_per_second: float | None = None
    impact_horizontal_mm: float | None = None
    impact_vertical_mm: float | None = None

    def __post_init__(self) -> None:
        _require_finite(
            "ClubDelivery",
            speed_mph=self.speed_mph,
            speed_at_impact_mph=self.speed_at_impact_mph,
            angle_of_attack_degrees=self.angle_of_attack_degrees,
            path_degrees=self.path_degrees,
            face_to_target_degrees=self.face_to_target_degrees,
            loft_degrees=self.loft_degrees,
            lie_degrees=self.lie_degrees,
            closure_rate_degrees_per_second=self.closure_rate_degrees_per_second,
            impact_horizontal_mm=self.impact_horizontal_mm,
            impact_vertical_mm=self.impact_vertical_mm,
        )

    @property
    def face_to_path_degrees(self) -> float | None:
        """Face relative to path, the number that explains curvature.

        Derived here rather than at the boundary: it is golf, so it belongs in
        the domain, and it is only meaningful when both inputs are present.
        """

        if self.face_to_target_degrees is None or self.path_degrees is None:
            return None
        return self.face_to_target_degrees - self.path_degrees

    @property
    def is_empty(self) -> bool:
        """True when a device sent a club block containing nothing usable."""

        return all(
            getattr(self, field) is None
            for field in (
                "speed_mph",
                "speed_at_impact_mph",
                "angle_of_attack_degrees",
                "path_degrees",
                "face_to_target_degrees",
                "loft_degrees",
                "lie_degrees",
                "closure_rate_degrees_per_second",
                "impact_horizontal_mm",
                "impact_vertical_mm",
            )
        )


@dataclass(frozen=True, slots=True)
class ShotSource:
    """Where a shot came from and when CaddieJ first saw it.

    Provenance is kept beside the shot rather than inside it, so a shot stays
    pure golf data. The ingestion timestamps live here because a shot must be
    timestamped when it enters the system, not when a subscriber reaches it.
    """

    kind: Literal["gspro_connect", "replay", "synthetic"]
    device_session_id: UUID
    observed_at: datetime
    observed_monotonic: float
    device_id: str | None = None
    device_sequence: int | None = None
    reported_units: str | None = None
    api_version: str | None = None
    raw_payload: str | None = None
    """Exactly what the device said, kept only when raw capture is switched on.

    This is opaque. Nothing may parse it, branch on it, or grow a feature that
    needs it: it exists so that a device behaving oddly can be diagnosed from a
    recorded round instead of a live one. It has no schema and no stability
    guarantee, its shape is whatever that device's vendor chose, and deleting
    every one of them must never break anything.
    """


@dataclass(frozen=True, slots=True)
class Shot:
    """One shot, as CaddieJ owns it.

    `club` is empty as ingested: no launch monitor names the club in its shot
    message, so correlating the club in play with the shot is domain work.
    """

    shot_id: UUID
    source: ShotSource
    ball: BallLaunch | None = None
    club_delivery: ClubDelivery | None = None
    handedness: Handedness = Handedness.UNKNOWN
    club: Club | None = None

    def __post_init__(self) -> None:
        if self.ball is None and self.club_delivery is None:
            raise ValueError("A Shot must carry ball data, club data, or both")

    @property
    def has_ball_data(self) -> bool:
        return self.ball is not None

    @property
    def has_club_data(self) -> bool:
        return self.club_delivery is not None
