"""Turning a measurement into a brightness, and nothing else.

Pure arithmetic, no lights, no clock, no network. Everything in this file can be
checked by reading it and proved by calling it, which is deliberate: it holds the
part of the behaviour somebody will want to argue about — how strong the effect
is — and that argument is much easier against a function than against a room.

The one idea worth explaining is that brightness here is always **a fraction of
the baseline**, never an absolute value. The baseline is whatever the lights were
doing when CaddieJ started talking, and it is the only honest reference point:
somebody sitting at twenty per cent in the evening and somebody at full daylight
both asked for that, and a reaction that ignored it would either be invisible to
one of them or blinding to the other. Working in fractions means the same
configuration is subtle in both rooms.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

from caddiej.domain.speech import Envelope

Curve = Callable[[float], float]
"""How bright the room should be, as a fraction of baseline, that many seconds in.

The single thing the service replays. Speech supplies one built from a measured
envelope and a cue supplies one built from a fixed shape, which is why the
service that drives them needs to know about neither.
"""


@dataclass(frozen=True, slots=True)
class Modulation:
    """How far the lights are allowed to move, and in which direction.

    Two numbers, both fractions of baseline, and the defaults are chosen to be
    almost too subtle rather than nearly too much. A room that visibly pulses
    while a coach talks is a novelty that gets switched off within a week; a room
    that seems very slightly alive is one nobody consciously notices and nobody
    turns off.

    `maximum` at exactly one is not an accident either. It means the loudest part
    of a sentence is the brightness the room was already at, so the effect is
    entirely a gentle dip in the gaps between words rather than a lift on top of
    them. That direction has a practical advantage worth more than the symmetry:
    dipping has room to work at any baseline, while lifting has none at all when
    the lights are already full.
    """

    minimum: float = 0.82
    maximum: float = 1.0

    def __post_init__(self) -> None:
        if self.minimum <= 0.0:
            raise ValueError("minimum must be above zero")
        if self.maximum < self.minimum:
            raise ValueError("maximum must not be below minimum")
        if self.maximum > 2.0:
            # Not a real ceiling — a bridge clamps anyway — but a typo that
            # doubles a room's brightness should be refused where it can still be
            # traced to a configuration file.
            raise ValueError("maximum must not exceed twice the baseline")

    @property
    def is_flat(self) -> bool:
        """Whether this would move anything. Configuration, not a special case."""

        return self.minimum == self.maximum

    @property
    def depth(self) -> float:
        """How much of the baseline the whole range spans."""

        return self.maximum - self.minimum

    def brightness(self, level: float) -> float:
        """The fraction of baseline for one measured level.

        Linear in the level, which is already the perceptual quantity: the
        envelope it comes from is an RMS measurement, so the curve from a
        listener's sense of loudness to a viewer's sense of brightness has
        already been applied once, and applying a second one turns a subtle
        effect into a lazy one.
        """

        clamped = min(1.0, max(0.0, level))
        return self.minimum + self.depth * clamped


def speaking_curve(envelope: Envelope, modulation: Modulation) -> Curve:
    """A curve that follows a voice.

    The envelope is sampled on the caller's clock rather than the analysis
    frame rate, which is what lets the lights update as slowly as a bridge
    requires while still tracking the words.
    """

    return lambda seconds: modulation.brightness(envelope.at(seconds))


@dataclass(frozen=True, slots=True)
class Flourish:
    """One gesture: how far the room moves, and for how long.

    The whole of what this subsystem accepts as a deliberate reaction, and
    deliberately not much. `target` is a fraction of the baseline brightness,
    exactly as in `Modulation`, so a flourish is subtle in a dim room and subtle
    in a bright one.

    Two dimensions, because two are what a room has without colour, and colour
    belongs to the parts of a lighting system CaddieJ does not touch. They turn
    out to be enough: depth and length are what make one gesture feel like more
    of an occasion than another.

    Nothing here knows why it is being asked. What a birdie is worth is a
    question for whatever orchestrates the reaction — see `caddiej.effects` —
    and keeping that judgement out of this package is what stops the lights from
    having their own opinion about golf.
    """

    target: float
    seconds: float

    def __post_init__(self) -> None:
        if self.target <= 0.0:
            raise ValueError("target must be above zero")
        if self.target > 2.0:
            raise ValueError("target must not exceed twice the baseline")
        if self.seconds <= 0.0:
            raise ValueError("a flourish must last some time")

    @property
    def curve(self) -> Curve:
        return pulse_curve(self.target, self.seconds)


def pulse_curve(target: float, seconds: float) -> Curve:
    """A single smooth departure from baseline and back.

    Half a sine, which is the least interesting shape that has no corners: it
    leaves baseline at zero, reaches `target` at the middle and returns. No
    corners matters, because a bridge interpolating between two commands across a
    corner produces a visible tick.

    Outside the window it sits at baseline, so a caller that runs it slightly
    long or samples it slightly late gets the room back rather than a jump.
    """

    if seconds <= 0:
        raise ValueError("a pulse must last some time")

    def curve(elapsed: float) -> float:
        if not 0.0 <= elapsed <= seconds:
            return 1.0
        level = math.sin(math.pi * elapsed / seconds)
        return 1.0 + (target - 1.0) * level

    return curve
