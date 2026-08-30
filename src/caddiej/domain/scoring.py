"""How a hole finished, named in the language of golf.

This is a vocabulary and nothing else. There is no brightness here, no sound, no
text on a floor and no scene in OBS, because those are answers to "how the room
should react" and this file only says "what happened". An effects system reads
it; so could a stored round, a leaderboard, or nothing at all.

`HoleCompleted` is **published by nobody yet**, and that is worth stating plainly
rather than leaving to be discovered. Recognising a birdie needs a hole, a par
and a stroke count, and CaddieJ has none of those: GSPro's Open Connect feed
carries shots and a club, not a scorecard. So this is the seam that scoring will
plug into when it exists, defined now because the five names below are the whole
of what any reaction needs to know, and defining them early is what keeps every
output — floor, OBS, voice, lights — from growing its own private idea of what a
good hole looks like.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from caddiej.core.events import Event


class HoleResult(StrEnum):
    """How a hole came out, relative to its par.

    Five values, and only the last four are good news. Par is here because a
    round is mostly pars and a system that can only describe the exceptional has
    nothing to say about the ordinary — but see the effects catalogue for how
    little it says about it.

    Deliberately no bogey, double, or worse. Not an oversight: nothing in
    CaddieJ should be built to react to somebody playing badly, and a vocabulary
    that cannot name a disaster cannot later be wired to one by accident.

    Strings rather than numbers, because these end up in logs, in health and one
    day in a stored round, and `"eagle"` survives a schema change that `3` does
    not.
    """

    PAR = "par"
    """As expected. Most holes, for most players."""

    BIRDIE = "birdie"
    """One under. Common enough that a good player gets several a round, so this
    should be acknowledged rather than celebrated."""

    EAGLE = "eagle"
    """Two under. A genuinely good hole."""

    ALBATROSS = "albatross"
    """Three under. Most golfers never have one."""

    HOLE_IN_ONE = "hole_in_one"
    """The only one of these anybody phones somebody about."""


@dataclass(frozen=True, slots=True)
class ScoringEvent(Event):
    """Something that happened to a score rather than to a swing."""


@dataclass(frozen=True, slots=True)
class HoleCompleted(ScoringEvent):
    """A hole finished, and this is how.

    Published by nobody today; see this module's own note. Subscribed to
    already, so that the day something can recognise a birdie, the room reacts
    without a line being changed anywhere near a light or a projector.
    """

    result: HoleResult
    hole_number: int | None = None
    shot_id: UUID | None = None
    """The shot that finished the hole, when it is known. Carried so a reaction
    can be tied to something on the floor rather than floating free."""
