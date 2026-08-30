"""What an effect is: six values, each answering a different question.

The shape of this dataclass is the whole design of the subsystem, so it is worth
saying what each field is for and, more usefully, what none of them are.

**There is no per-channel section.** No brightness, no scene name, no font size,
no audio file. An effect says how much of an occasion something is and each
channel decides what that means in its own terms, because the alternative — an
effect carrying a slot per output — makes adding a fifth output an edit to every
effect, and makes the table below a place where somebody tunes a colour.

**`priority` and `intensity` are not the same number.** They correlate for
everything in the catalogue today, which is exactly why keeping them apart
matters: one decides who wins when two things happen at once, the other decides
how big the gesture is, and the day something is urgent but small — a warning,
say — a single field would have to be wrong about one of them.

**Duration is here rather than in a channel.** One effect, one length, decided in
one place, so the light, the label and the OBS source go away together. A channel
that timed itself would drift from the others and there would be nothing to
blame.
"""

from dataclasses import dataclass
from enum import IntEnum


class EffectPriority(IntEnum):
    """How much an effect deserves the room, when something is already using it.

    Ordered and spaced by ten, for the same reasons as `SpeechPriority`: ordered
    because replacement is decided by comparing two of these and nothing else,
    and spaced because the useful new priority is always between two existing
    ones, and renumbering a value that has been logged is how a quiet mistake
    gets made.

    Four classes rather than one per cue. A class is a statement about what may
    interrupt what, and there are only about four such statements worth making
    about a golf simulator; a scale with one entry per event would make the
    fifteenth event's precedence a question nobody can answer.
    """

    ROUTINE = 10
    """Worth acknowledging, not worth interrupting anything for."""

    NOTABLE = 20
    """Worth a moment of the room's attention."""

    RARE = 30
    """Worth taking the room from whatever else was using it."""

    LANDMARK = 40
    """Nothing outranks this. Reserved for things that happen once."""


@dataclass(frozen=True, slots=True)
class Effect:
    """One thing the room does, and how much of a thing it is."""

    cue: str
    """A stable machine name — `"birdie"` — for logs, health and the browser's
    styling hook. Never shown to anybody as prose."""

    label: str
    """The words themselves, as the floor and an OBS caption show them. Short
    enough to read from standing height at a glance, because that is the only
    way anybody will read it."""

    priority: EffectPriority
    intensity: float
    """How much of an occasion this is, from nothing to everything.

    One dial, deliberately. Every channel scales its own gesture by it and
    documents its own mapping, which is what lets a channel be added without
    the catalogue learning a new vocabulary — and what stops the catalogue from
    becoming a place where somebody tunes a shade of green.
    """

    seconds: float
    """How long the whole effect lasts, across every channel at once."""

    say: str = ""
    """A short line for the voice, or empty for silence.

    Empty is a real answer and the catalogue uses it. Something that happens on
    most holes should not be announced on most holes; a room that comments on
    everything is a room somebody mutes.
    """

    def __post_init__(self) -> None:
        if not self.cue:
            raise ValueError("an effect needs a cue name")
        if not self.label:
            raise ValueError("an effect needs a label")
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError("intensity must lie between zero and one")
        if self.seconds <= 0.0:
            raise ValueError("an effect must last some time")
