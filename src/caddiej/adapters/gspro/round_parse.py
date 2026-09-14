"""Parse GSPro currentRound.dat into domain observations.

The file is UTF-8 JSON despite the `.dat` extension. Values are copied as raw
numbers and integers; units and enum meanings are not inferred.
"""

from __future__ import annotations

import json
import math
from typing import Any

from caddiej.domain.observation import (
    BallPathPoint,
    ObservedRoundShot,
    WorldPoint,
)


def parse_round_document(text: str) -> list[ObservedRoundShot] | None:
    """Return shots, or None when the document is not usable JSON yet.

    A partial write from GSPro is the expected failure: the caller retries.
    Unknown fields are ignored. Missing fields become None.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    shots: list[ObservedRoundShot] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        parsed = _shot_from_item(item)
        if parsed is not None:
            shots.append(parsed)
    return shots


def _shot_from_item(item: dict[str, Any]) -> ObservedRoundShot | None:
    shot_id = _as_id(item.get("ShotID"))
    if shot_id is None:
        return None
    active = item.get("activeShot")
    sd = None
    if isinstance(active, dict):
        raw_sd = active.get("sd")
        if isinstance(raw_sd, dict):
            sd = raw_sd
    ball_start = _point(sd.get("BallStartPos") if sd else None)
    return ObservedRoundShot(
        shot_id=shot_id,
        global_shot_number=_as_int(item.get("GlobalShotNumber")),
        player_name=_as_str(item.get("PlayerName")),
        hole=_as_int(item.get("Hole")),
        hole_par=_as_int(item.get("HolePar")),
        hole_shot=_as_int(item.get("HoleShot")),
        club_index_raw=_as_int(item.get("ClubIndex")),
        ball_speed_raw=_as_float(item.get("BallSpeed")),
        distance_to_pin_raw=_as_float(item.get("DistanceToPin")),
        starting_surface_raw=_as_int(item.get("StartingSurface")),
        ending_surface_raw=_as_int(item.get("EndingSurface")),
        total_distance_raw=_as_float(item.get("TotalDistance")),
        shot_result_raw=_as_int(item.get("ShotResult")),
        hole_result_raw=_as_int(item.get("HoleResult")),
        starting_position=_point(item.get("StartingPOS")),
        ending_position=_point(item.get("EndingPOS")),
        is_putt=_as_bool(sd.get("isPutt") if sd else None),
        is_holed=_as_bool(sd.get("isHoled") if sd else None),
        target_direction_raw=_as_float(sd.get("TargetDirection") if sd else None),
        final_speed_raw=_as_float(sd.get("finalSpeed") if sd else None),
        final_spin_raw=_as_float(sd.get("finalSpin") if sd else None),
        ball_start=ball_start,
        ball_path=_path(sd.get("path") if sd else None),
    )


def _path(value: object) -> tuple[BallPathPoint, ...]:
    if not isinstance(value, list):
        return ()
    points: list[BallPathPoint] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        time = _as_float(item.get("time"))
        pos = _point(item.get("pos"))
        if time is None or pos is None:
            continue
        points.append(BallPathPoint(time=time, x=pos.x, y=pos.y, z=pos.z))
    return tuple(points)


def _point(value: object) -> WorldPoint | None:
    if not isinstance(value, dict):
        return None
    x = _as_float(value.get("x"))
    y = _as_float(value.get("y"))
    z = _as_float(value.get("z"))
    if x is None or y is None or z is None:
        return None
    return WorldPoint(x=x, y=y, z=z)


def _as_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int):
        return str(value)
    return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    return None
