"""Round-play observations that are not a launch-monitor Shot.

A `Shot` carries named units (mph, yards, rpm) because Open Connect values are
translated at that boundary. GSPro's `currentRound.dat` has been read on real
hardware, but its numeric units have not been verified. Those numbers stay raw
here so a later source can enrich a shot with launch-monitor metrics without
this result having pretended to be one.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorldPoint:
    """A point as GSPro stored it. Axes and units are unverified."""

    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class BallPathPoint:
    """One sample of ball flight, not club path.

    `time` and `x`/`y`/`z` are the values GSPro wrote. They are not metres on
    the physical floor and are not fed into floor calibration.
    """

    time: float
    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class ObservedRoundShot:
    """A completed GSPro round-play shot, as currentRound.dat reported it.

    Integer enums (`ClubIndex`, surfaces, results) stay raw. Numeric fields
    whose units have not been verified stay `*_raw`. Practice Range is not
    represented: this file was observed to update during round play only.
    """

    shot_id: str
    global_shot_number: int | None = None
    player_name: str | None = None
    hole: int | None = None
    hole_par: int | None = None
    hole_shot: int | None = None
    club_index_raw: int | None = None
    ball_speed_raw: float | None = None
    distance_to_pin_raw: float | None = None
    starting_surface_raw: int | None = None
    ending_surface_raw: int | None = None
    total_distance_raw: float | None = None
    shot_result_raw: int | None = None
    hole_result_raw: int | None = None
    starting_position: WorldPoint | None = None
    ending_position: WorldPoint | None = None
    is_putt: bool | None = None
    is_holed: bool | None = None
    target_direction_raw: float | None = None
    final_speed_raw: float | None = None
    final_spin_raw: float | None = None
    ball_start: WorldPoint | None = None
    ball_path: tuple[BallPathPoint, ...] = ()
