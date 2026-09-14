"""Verified ProTee main-log patterns, and nothing else."""

from __future__ import annotations

import re

from caddiej.core.events import Event
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    HandednessChanged,
    LaunchMonitorReadinessChanged,
    ShotDispatched,
)
from caddiej.domain.shot import Club, ClubKind, Handedness

_SOURCE = "protee_log"

_READY = re.compile(r"VX status changed:\s*READY FOR SHOT", re.IGNORECASE)
_FINDING = re.compile(r"VX status changed:\s*FINDING BALL", re.IGNORECASE)
_CLUB = re.compile(
    r"Detected a (right|left) handed (.+)$",
    re.IGNORECASE,
)
_SENT = re.compile(r"Sending shot to game:", re.IGNORECASE)
_BALL_POS = re.compile(r"relative ballPos", re.IGNORECASE)

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
    if _SENT.search(text):
        events.append(ShotDispatched(source=_SOURCE))
    if _BALL_POS.search(text):
        events.append(BallDetected(source=_SOURCE))
    return tuple(events)


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
