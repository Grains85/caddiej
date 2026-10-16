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


def format_remaining_distance_m(distance_to_pin_raw: float | None) -> str | None:
    """Integer metres left to the pin after the shot, or None to omit the panel.

    ``currentRound.dat`` ``DistanceToPin`` on a shot record is the distance
    remaining once **that** shot has come to rest, not the distance the player
    was playing from. Verified on a real round (2026-09-16, hole 1) two ways:

    * arithmetically along the hole — 158.3 m left, a 5.9 m shot, 153.1 m left;
      then a 175.3 m shot from 153.1 m out leaving 23.1 m, which is the overshoot;
    * against the other source — ProTee logged GSPro Code 201
      ``DistanceToTarget`` 10.9 then 9.0 in the two seconds after a chip, matching
      the previous shot's ``DistanceToPin`` 10.855 and the new shot's 9.008.

    Same metres as ``TotalDistance``, from the same arithmetic. Non-positive is
    omitted rather than shown as ``0 m``: it has not been observed on real
    hardware, so what GSPro writes for a holed shot is not established, and the
    floor does not put an unverified number in front of a player.
    """

    if distance_to_pin_raw is None or distance_to_pin_raw <= 0:
        return None
    return format_total_distance_m(distance_to_pin_raw)


def format_target_distance_m(distance_raw: float | None) -> str | None:
    """Integer metres for live ``DistanceToTarget``.

    Hardware: after a shot, Code 201 ``DistanceToTarget`` 289.4 matched
    ``currentRound.dat`` ``DistanceToPin`` 289.443. ``TotalDistance`` from
    the same file is already shown as metres. Non-positive values are the
    GSPro handshake empty target and are omitted.
    """

    if distance_raw is None or distance_raw <= 0:
        return None
    return format_total_distance_m(distance_raw)


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
