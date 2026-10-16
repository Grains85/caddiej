"""Verified ProTee main-log patterns, and nothing else."""

from __future__ import annotations

import json
import math
import re

from caddiej.core.events import Event
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    HandednessChanged,
    LaunchMonitorReadinessChanged,
    ShotDetected,
    ShotDispatched,
    TargetDistanceObserved,
)
from caddiej.domain.shot import Club, ClubKind, Handedness

_SOURCE = "protee_log"

_READY = re.compile(r"VX status changed:\s*READY FOR SHOT", re.IGNORECASE)
_FINDING = re.compile(r"VX status changed:\s*FINDING BALL", re.IGNORECASE)
# The club name runs to the end of the message, and NLog puts a field
# terminator after the message: real lines read `...handed Iron|`. Without the
# optional pipe the vendor code was `Iron|`, which reached the floor as the
# caption `IRON|`.
_CLUB = re.compile(
    r"Detected a (right|left) handed (.+?)\s*\|?\s*$",
    re.IGNORECASE,
)
_SENT = re.compile(r"Sending shot to game:", re.IGNORECASE)
# The camera trigger, and the earliest per-shot line in the log. A trigger
# streams N buffer images; only the first is matched, so one strike produces
# one event. Measured on a full session of 16 shots (2026-09-16): every trigger
# was followed by a send 77-385 ms later, and no send occurred without one.
# Deliberately not "Sending shot to game", which the same session wrote twice
# for 5 of those shots (ball-only, then club-only) and 150 ms later.
_TRIGGER = re.compile(
    r"Received trigger\s+(\d+)\s+buffer image 1 of the\s+\d+",
    re.IGNORECASE,
)
_BALL_POS = re.compile(r"relative ballPos", re.IGNORECASE)
# NLog writes `<stamp>|INFO|<thread>|<message>|` — the message is *followed* by a
# field terminator, so the JSON is not at the end of the line. Anchoring on `$`
# matched every fixture written by hand and nothing ProTee has ever logged: three
# days of real logs (2026-09-14/15/16, 300 Code 201 lines) produced no target
# distance at all. Greedy to the last brace on the line instead, and a payload
# that is not JSON is dropped by the parse below rather than guessed at.
_GSPRO_READ = re.compile(r"GSPro read data:\s*(\{.+\})", re.IGNORECASE)

_WEDGE_WORDS = frozenset(
    {"wedge", "pw", "sw", "lw", "gw", "aw", "sand", "pitching", "lob", "gap"}
)


def events_from_line(line: str) -> tuple[Event, ...]:
    """Parse one complete log line into domain events.

    Only patterns verified on real hardware are recognised. Anything else is
    ignored rather than guessed at.
    """

    text = line.strip()
    if not text:
        return ()
    events: list[Event] = []
    if _READY.search(text):
        events.append(LaunchMonitorReadinessChanged(ready=True, source=_SOURCE))
    if _FINDING.search(text):
        events.append(LaunchMonitorReadinessChanged(ready=False, source=_SOURCE))
        events.append(BallCleared(source=_SOURCE))
    club_match = _CLUB.search(text)
    if club_match is not None:
        handed = club_match.group(1).lower()
        name = club_match.group(2).strip()
        events.append(
            HandednessChanged(
                handedness=(Handedness.RIGHT if handed == "right" else Handedness.LEFT),
                source=_SOURCE,
            )
        )
        events.append(ClubSelected(club=_club_from_name(name), source=_SOURCE))
    trigger = _TRIGGER.search(text)
    if trigger is not None:
        events.append(ShotDetected(trigger_id=trigger.group(1), source=_SOURCE))
    if _SENT.search(text):
        events.append(ShotDispatched(source=_SOURCE))
    if _BALL_POS.search(text):
        events.append(BallDetected(source=_SOURCE))
    target = _target_distance_from_line(text)
    if target is not None:
        events.append(TargetDistanceObserved(distance_raw=target, source=_SOURCE))
    return tuple(events)


def _target_distance_from_line(text: str) -> float | None:
    """Live remaining distance from a GSPro Code 201 player-info payload.

    ProTee already received this JSON from GSPro and wrote it to the semantic
    log. This is a read of that line, not an intercept of the Open Connect
    socket. ``Surface`` has only been observed as JSON null, so it is ignored.
    A non-positive ``DistanceToTarget`` is the handshake empty value, not a
    real target.

    Metres, and the distance the *next* shot will be played from. Verified on
    the real log of 2026-09-16: GSPro pushed ``DistanceToTarget`` 16.8 at
    12:57:24.2, five seconds after the 12:57:19.0 camera trigger, and
    ``currentRound.dat`` recorded ``DistanceToPin`` 16.7867889 for that same
    shot. The whole session walks a hole in: 485.1, 428.6, 374.9, 314.0, 234.6,
    160.4, 66.3, 30.3, 3.2.

    ``Player.Club`` on the same payload is GSPro's own auto-club and is
    deliberately ignored: which club to hit is CaddieJ's recommendation to make,
    from the player's measured carries, not a value to mirror.
    """

    match = _GSPRO_READ.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("Code") != 201:
        return None
    player = payload.get("Player")
    if not isinstance(player, dict):
        return None
    raw = player.get("DistanceToTarget")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    distance = float(raw)
    if not math.isfinite(distance) or distance <= 0.0:
        return None
    return distance


def _club_from_name(name: str) -> Club:
    kind = _club_kind(name)
    return Club(kind=kind, vendor_code=name)


def _club_kind(name: str) -> ClubKind:
    lowered = name.strip().lower()
    if "driver" in lowered:
        return ClubKind.DRIVER
    if "putter" in lowered:
        return ClubKind.PUTTER
    if "wood" in lowered:
        return ClubKind.WOOD
    if "hybrid" in lowered or "rescue" in lowered:
        return ClubKind.HYBRID
    if "iron" in lowered:
        return ClubKind.IRON
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
    if tokens & _WEDGE_WORDS:
        return ClubKind.WEDGE
    return ClubKind.UNKNOWN
