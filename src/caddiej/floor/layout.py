"""Where golf content sits, expressed relative to the hitting strip.

The strip is the one thing in the room that is physically real and physically
moveable: somebody lays a mat down, aims a projector at it, and measures it.
Everything golf-related on the floor is positioned *from* it —

    projector calibration → metres on the bay → strip anchor → element offset

— so recalibrating a bay is moving one rectangle rather than re-measuring every
overlay element in turn. Offsets are metres on the same physical plane as
`PhysicalRect`, and they are resolved to `PhysicalPoint` here, before anything
is mapped onto the calibrated surface. Nothing in this module knows about
pixels, and nothing about it changes with the projector's resolution.

Not everything is anchored. The startup banner, the celebration layer and the
link indicator are drawn across the whole surface and have nothing to do with a
mat, so they stay where they are. What is anchored is what a player reads while
standing at the strip: where the ball goes, how far is left, which club that
calls for, and what the last shot did.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import PhysicalPoint, PhysicalRect

# How far below the information anchor the coaching line sits, in metres. Not
# configurable: it is the gap between two things that are drawn together, not a
# fact about anybody's room, and a bay that wants the numbers somewhere else
# moves the information offset and takes the line with it.
ADVICE_GAP_M = 0.20

# Where the putter ball goes when nobody has said. Deliberately not the iron
# point: a putter is not a normal club, and the whole reason this mode exists is
# that a putt is played from somewhere else. Negative is down-range, so the
# default is off the front edge of any sane strip — off the mat, which is
# allowed for a putter and is the point.
DEFAULT_PUTTER_DY_M = -0.30


class PlacementMode(StrEnum):
    """Which of the three ball positions a club calls for.

    Three, not one per club kind. A 7 iron, a wedge and a hybrid are all played
    from the same spot on a mat; a driver and a putter are not. Appearance is a
    separate decision (`marker_variant_for`), because a bay may want the putter
    drawn differently without moving it, or the other way round.
    """

    IRON = "iron"
    DRIVER = "driver"
    PUTTER = "putter"


_MODES = {
    ClubKind.DRIVER: PlacementMode.DRIVER,
    ClubKind.PUTTER: PlacementMode.PUTTER,
}


def placement_mode_for(club: Club | None) -> PlacementMode:
    """Deterministic, total, and biased towards the normal case.

    A club nobody has named, and a kind nothing special was configured for,
    both play from the iron position. That is the fallback because it is the
    one that is right most often and wrong least badly: a ball placed at the
    iron point when a driver was in the player's hands is a ball a few
    centimetres out, whereas a driver point used for a wedge is a ball nowhere
    near the middle of the mat.
    """

    if club is None:
        return PlacementMode.IRON
    return _MODES.get(club.kind, PlacementMode.IRON)


@dataclass(frozen=True, slots=True)
class PhysicalOffset:
    """A displacement in metres on the hitting bay.

    Same axes as `PhysicalPoint`: +X across the bay towards the player's right,
    +Y towards the player. Down-range is −Y.
    """

    dx_m: float = 0.0
    dy_m: float = 0.0

    def __post_init__(self) -> None:
        for name in ("dx_m", "dy_m"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"PhysicalOffset.{name} must be a finite number")


NO_OFFSET = PhysicalOffset()

# Where a bay that has told CaddieJ nothing is assumed to tee the ball, and how
# big a generic patch of turf around it is. Not a measured room: an operator
# who has measured theirs sets `FLOOR_HITTING_STRIP_*` and none of this is used.
DEFAULT_TEE = PhysicalPoint(x_m=0.5, y_m=0.72)
DEFAULT_STRIP_WIDTH_M = 0.30
DEFAULT_STRIP_DEPTH_M = 0.45


@dataclass(frozen=True, slots=True)
class FloorLayout:
    """The strip, and everything positioned from it.

    `strip` is absolute on the bay because it is the one measurement somebody
    actually takes. Everything else is an offset from its centre, so moving the
    mat — or moving the projector and re-measuring — is one edit.

    A mode with no offset of its own falls back to `PlacementMode.IRON`, except
    the putter, which falls back to a point deliberately off the strip rather
    than to the iron point.
    """

    strip: PhysicalRect
    ball: Mapping[PlacementMode, PhysicalOffset] = field(default_factory=dict)
    information: PhysicalOffset = NO_OFFSET
    strip_filled: bool = False
    putting_lane: bool = True
    putting_lane_length_m: float = 0.30
    putting_lane_width_m: float = 0.24

    def __post_init__(self) -> None:
        for name in ("putting_lane_length_m", "putting_lane_width_m"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"FloorLayout.{name} must be a positive finite number")

    @property
    def anchor(self) -> PhysicalPoint:
        """The strip's physical centre. Everything below hangs off this."""

        return PhysicalPoint(x_m=self.strip.x_m, y_m=self.strip.y_m)

    def resolve(self, offset: PhysicalOffset) -> PhysicalPoint:
        """`hitting_strip_position + element_offset`, in metres."""

        return PhysicalPoint(
            x_m=self.strip.x_m + offset.dx_m,
            y_m=self.strip.y_m + offset.dy_m,
        )

    def offset_for(self, mode: PlacementMode) -> PhysicalOffset:
        configured = self.ball.get(mode)
        if configured is not None:
            return configured
        if mode is PlacementMode.PUTTER:
            return PhysicalOffset(dy_m=DEFAULT_PUTTER_DY_M)
        return self.ball.get(PlacementMode.IRON, NO_OFFSET)

    def ball_position(self, mode: PlacementMode) -> PhysicalPoint:
        """Where the ball goes for this placement mode, in metres."""

        return self.resolve(self.offset_for(mode))

    def ball_position_for(self, club: Club | None) -> PhysicalPoint:
        return self.ball_position(placement_mode_for(club))

    def information_position(self) -> PhysicalPoint:
        """Where the one information area is drawn from."""

        return self.resolve(self.information)

    def advice_position(self) -> PhysicalPoint:
        """Below the information area, towards the player.

        Derived rather than configured: the coaching line belongs under the
        numbers, and a bay that moves the numbers has already said where it
        wants both.
        """

        return self.resolve(
            PhysicalOffset(
                dx_m=self.information.dx_m,
                dy_m=self.information.dy_m + ADVICE_GAP_M,
            )
        )

    def putting_lane_rect(self) -> PhysicalRect:
        """The guide, as a rectangle running down-range from the putter ball.

        Down-range is −Y, the same direction a club path of 0° is drawn in, so
        the lane and every other physical ray on this floor agree about which
        way the screen is.
        """

        ball = self.ball_position(PlacementMode.PUTTER)
        return PhysicalRect(
            x_m=ball.x_m,
            y_m=ball.y_m - self.putting_lane_length_m / 2.0,
            width_m=self.putting_lane_width_m,
            depth_m=self.putting_lane_length_m,
        )

    def putting_lane_end(self) -> PhysicalPoint:
        """The far end of the lane's centre line."""

        ball = self.ball_position(PlacementMode.PUTTER)
        return PhysicalPoint(x_m=ball.x_m, y_m=ball.y_m - self.putting_lane_length_m)


def layout_from_tees(
    strip: PhysicalRect | None = None,
    *,
    tee: PhysicalPoint = DEFAULT_TEE,
    driver_tee: PhysicalPoint | None = None,
    putter_tee: PhysicalPoint | None = None,
    information: PhysicalOffset = NO_OFFSET,
    strip_filled: bool = False,
    putting_lane: bool = True,
    putting_lane_length_m: float = 0.30,
    putting_lane_width_m: float = 0.24,
) -> FloorLayout:
    """A layout from absolute tee points, by subtracting the anchor.

    The one bridge for callers that think in absolute metres — the deprecated
    `FLOOR_TEE_*_M` variables, and the tests and demos that were written
    against them. The result is the same offsets an operator would have
    configured, so there is one coordinate model below this line rather than
    two. `FloorPresenter` used to carry a second copy of this conversion and
    accept the absolute points directly, which meant the presenter had two
    coordinate models and most of its tests exercised the one production does
    not use.

    A strip that is not given is a generic patch of turf at the tee. Nobody
    has measured that room; an operator who has sets `FLOOR_HITTING_STRIP_*`.
    """

    if strip is None:
        strip = PhysicalRect(
            x_m=tee.x_m,
            y_m=tee.y_m,
            width_m=DEFAULT_STRIP_WIDTH_M,
            depth_m=DEFAULT_STRIP_DEPTH_M,
        )

    def offset(point: PhysicalPoint) -> PhysicalOffset:
        return PhysicalOffset(dx_m=point.x_m - strip.x_m, dy_m=point.y_m - strip.y_m)

    ball: dict[PlacementMode, PhysicalOffset] = {PlacementMode.IRON: offset(tee)}
    if driver_tee is not None:
        ball[PlacementMode.DRIVER] = offset(driver_tee)
    if putter_tee is not None:
        ball[PlacementMode.PUTTER] = offset(putter_tee)
    return FloorLayout(
        strip=strip,
        ball=ball,
        information=information,
        strip_filled=strip_filled,
        putting_lane=putting_lane,
        putting_lane_length_m=putting_lane_length_m,
        putting_lane_width_m=putting_lane_width_m,
    )
