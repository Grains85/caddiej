"""Turning a GSPro round entry into the roll a putt actually made, or refusing.

What this can honestly draw, and what it cannot, was settled by reading a real
`currentRound.dat` off the hitting bay — 196 shots, of which 191 were under
twelve metres. Three things in that file decide the shape of this module:

* `activeShot.sd.path` is a **rolling buffer of the tail of the trajectory**,
  not the whole of it. The longest shot in the capture travelled 101 m and
  carried thirteen samples, the first of which was already 95 m down-range. The
  last sample is always exactly `EndingPOS`, so the buffer ends at the shot; it
  does not begin at it.
* For short shots the buffer reaches back *before* the shot. The last entry in
  the capture moved 0.124 m and its path spans 0.807 m, including a 0.17 m drop
  in `pos.y` — a ball being placed on the green, not a ball being rolled.
* `isPutt` was `false` on all 196, `TargetDirection` was `0.0` on all 196, and
  `BallStopPos` was `(0, 0, 0)` on all 196.

So the buffer is only usable when the whole of the shot fits inside it and
nothing but the shot is in it, and that is exactly what the checks below test
for. A refusal is returned with a reason rather than an approximation, because
a line on a floor is read as a measurement whether or not it is one.

Orientation is the one assumption here and it is stated rather than hidden.
GSPro is a Unity game and Unity's world is left-handed with +y up, so from
above, rotating world forward by ninety degrees clockwise gives world right.
The trace is drawn with the ball's **initial roll direction** down-range, so
what the floor shows is the roll relative to the line the ball set off on:
distances are measured, and only the side a break falls on depends on that
handedness. It is not an aim-referenced ball path, because nothing in the file
says where the player was aiming.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from caddiej.domain.observation import BallPathPoint, ObservedRoundShot, WorldPoint
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    FloorCalibration,
    PhysicalPoint,
)
from caddiej.floor.state import PuttPath

logger = logging.getLogger(__name__)

# A putt shorter than this is a tap-in. There is no shape in it worth drawing,
# and the sample spacing would make whatever was drawn mostly noise.
MIN_ROLL_M = 0.15

# How far the ball may leave the putting surface before this stops being a roll.
# The placement motion in the real capture dropped 0.17 m, so anything at that
# scale is rejected outright.
MAX_RISE_M = 0.05

# How far the buffer's first usable sample may sit from where GSPro says the
# shot started, and how far a sample may move back away from the hole before
# the trace stops being a roll towards it.
_START_TOLERANCE_M = 0.08
_BACKWARDS_TOLERANCE_M = 0.05

# Enough to show a break at standing height. More than this is a longer line
# made of shorter segments, which looks the same and costs every client a
# larger snapshot.
_MAX_POINTS = 48


@dataclass(frozen=True, slots=True)
class PuttTraceRefused:
    """Why no path was drawn, in one word an operator can grep the log for."""

    reason: str


def putt_trace(
    shot: ObservedRoundShot,
    ball: PhysicalPoint,
    calibration: FloorCalibration | None = None,
) -> PuttPath | PuttTraceRefused:
    """The measured roll, placed at `ball` and rotated onto its own start line.

    `ball` is where the putter marker is, in metres, so the trace begins under
    the ball the player just hit rather than wherever GSPro's world origin is.
    """

    mapping = DEFAULT_CALIBRATION if calibration is None else calibration
    start = shot.starting_position
    end = shot.ending_position
    if start is None or end is None:
        return PuttTraceRefused("no start or end position")
    samples = shot.ball_path
    if len(samples) < 3:
        return PuttTraceRefused("fewer than three path samples")

    roll_m = _plan_distance(start, end)
    if roll_m < MIN_ROLL_M:
        return PuttTraceRefused("the ball barely moved")

    usable = _within_reach(samples, end, roll_m)
    if len(usable) < 3:
        return PuttTraceRefused("the path buffer does not hold the whole roll")
    if _plan_distance(usable[0], start) > _START_TOLERANCE_M:
        return PuttTraceRefused("the path buffer does not start at the shot")
    if _rise(usable) > MAX_RISE_M:
        return PuttTraceRefused("the ball left the surface")
    if not _rolls_towards(usable, end):
        return PuttTraceRefused("the path does not roll towards where it stopped")

    forward = _unit(usable[0], usable[1])
    if forward is None:
        return PuttTraceRefused("no start direction")

    trimmed = _thin(usable, _MAX_POINTS)
    origin = trimmed[0]
    fx, fz = forward
    points = []
    for sample in trimmed:
        dx = sample.x - origin.x
        dz = sample.z - origin.z
        along = dx * fx + dz * fz
        # Unity's world is left-handed with +y up, so world right is world
        # forward turned ninety degrees clockwise seen from above.
        sideways = dx * fz - dz * fx
        points.append(
            mapping.to_surface(
                PhysicalPoint(x_m=ball.x_m + sideways, y_m=ball.y_m - along)
            )
        )
    return PuttPath(points=tuple(points))


def _within_reach(
    samples: Sequence[BallPathPoint], end: WorldPoint, roll_m: float
) -> tuple[BallPathPoint, ...]:
    """The tail of the buffer that this shot could have produced.

    The buffer runs on past the previous shot and its last sample is always
    where this one stopped, so the shot is whatever lies within its own length
    of that resting place. Everything before is somebody else's roll, or a ball
    being put down.
    """

    reach = roll_m + max(_START_TOLERANCE_M, 0.1 * roll_m)
    kept: list[BallPathPoint] = []
    for sample in samples:
        if _plan_distance(sample, end) <= reach:
            kept.append(sample)
        else:
            # Anything earlier belongs to whatever the buffer held before.
            kept.clear()
    return tuple(kept)


def _rolls_towards(samples: Sequence[BallPathPoint], end: WorldPoint) -> bool:
    remaining = [_plan_distance(sample, end) for sample in samples]
    return all(
        later <= earlier + _BACKWARDS_TOLERANCE_M
        for earlier, later in zip(remaining, remaining[1:], strict=False)
    )


def _rise(samples: Sequence[BallPathPoint]) -> float:
    heights = [sample.y for sample in samples]
    return max(heights) - min(heights)


def _unit(first: BallPathPoint, second: BallPathPoint) -> tuple[float, float] | None:
    dx = second.x - first.x
    dz = second.z - first.z
    length = math.hypot(dx, dz)
    if length < 1e-6:
        return None
    return dx / length, dz / length


def _thin(samples: Sequence[BallPathPoint], limit: int) -> tuple[BallPathPoint, ...]:
    if len(samples) <= limit:
        return tuple(samples)
    step = len(samples) / limit
    thinned = [samples[int(index * step)] for index in range(limit)]
    # The resting place is the one sample that must survive thinning: it is the
    # only point on the trace a player can check against the ball.
    if thinned[-1] is not samples[-1]:
        thinned[-1] = samples[-1]
    return tuple(thinned)


def _plan_distance(
    a: BallPathPoint | WorldPoint, b: BallPathPoint | WorldPoint
) -> float:
    """Distance ignoring height. GSPro's y is up; a putt is read from above."""

    return math.hypot(a.x - b.x, a.z - b.z)
