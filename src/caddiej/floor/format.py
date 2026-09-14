"""HUD-only formatting for GSPro round-play numbers.

Domain values stay raw. Nothing here is written back onto an
``ObservedRoundShot``. The conversions are hardware-derived presentation
assumptions from the first hitting-bay session, not a GSPro specification.

BallSpeed: a real ``currentRound.dat`` value of about 32.8386 displayed as a
drive of about 118 km/h, which is metres per second × 3.6.

TotalDistance: the same session treated the number as metres of the shot.
``currentRound.dat`` does not name a unit. Nothing in the repository
contradicts metres, so the overlay labels it ``m``. World-position fields
(``StartingPOS``, ``BallStartPos``) are a different, still-unverified space
and are not formatted here.
"""

from __future__ import annotations

import math

# Hardware-derived: currentRound.dat BallSpeed is metres per second.
_MPS_TO_KMH = 3.6


def format_ball_speed_kmh(ball_speed_raw: float | None) -> str | None:
    """Integer km/h for the overlay, or None when there is no finite speed."""

    kmh = _scaled_int(ball_speed_raw, _MPS_TO_KMH)
    if kmh is None:
        return None
    return f"{kmh} km/h"


def format_total_distance_m(total_distance_raw: float | None) -> str | None:
    """Integer metres for the overlay, or None when there is no finite distance."""

    metres = _nearest_int(total_distance_raw)
    if metres is None:
        return None
    return f"{metres} m"


def _scaled_int(value: float | None, factor: float) -> int | None:
    if value is None:
        return None
    return _nearest_int(value * factor)


def _nearest_int(value: float | None) -> int | None:
    """Half away from zero, so 248.5 becomes 249 rather than banker's 248."""

    if value is None or not math.isfinite(value):
        return None
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))
