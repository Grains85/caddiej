"""What CaddieJ wants said out loud, and what became of it.

An `Utterance` is a request to speak, not a sound. It carries text, how urgent
the text is, and whether it goes stale — and nothing about voices, sample rates,
devices or files, because those are answers to "how", and the domain only asks
"what". That is what lets the whole speaking apparatus be absent, broken or
faked without anything upstream noticing.

Two ideas do the work.

**Priority is a number so that interruption is a comparison.** A louder thing
arriving while a quieter thing is playing needs exactly one rule to resolve it,
and `>` is that rule. The bands are spaced so that levels can be inserted
between them later without renumbering what already exists.

**Perishable says whether waiting ruins it.** A coaching line about the last
shot is worthless once another ball is in the air, so a newer one replaces it
while it waits. A future warning is not like that: two of them are two things
worth hearing. One flag, set by whoever asks for the speech, keeps that decision
with the thing that knows the answer.

`Envelope` is the one thing here that describes a sound rather than a request,
and it is here rather than in the speech package for one reason: it travels on
`SpeechStarted`, and the domain's events cannot import a subsystem. It is
deliberately not audio — it is a shape, in numbers anything can read.
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from uuid import UUID, uuid4


class SpeechPriority(IntEnum):
    """How much a line deserves the room's attention.

    Ordered and spaced by ten. Ordered because interruption is decided by
    comparing two of these and nothing else; spaced because the useful new
    priority is always between two existing ones, and renumbering a stored or
    logged value later is how a quiet mistake gets made.
    """

    COACHING = 10
    """A line about the shot just hit. The only priority anything currently
    produces. Interruptible, and perishable: a newer one makes it pointless."""

    ANNOUNCEMENT = 20
    """Something about the round rather than the swing. Reserved."""

    ALERT = 30
    """Must be heard, and worth cutting off whatever is playing. Reserved."""


class SpeechOutcome(StrEnum):
    """What happened to one request to speak.

    Recorded for every utterance, including the ones that never made a sound.
    "Nothing was said" and "nothing was asked" are different facts, and a room
    that has gone quiet is worth being able to explain.
    """

    SPOKEN = "spoken"
    """Played to the end."""

    INTERRUPTED = "interrupted"
    """Something more urgent arrived mid-sentence and took the speaker."""

    SUPERSEDED = "superseded"
    """A newer perishable line replaced it before it was ever played."""

    DROPPED = "dropped"
    """The queue was full and this was the least worth keeping."""

    UNAVAILABLE = "unavailable"
    """Nothing could speak it: no engine, no output device, or switched off."""

    FAILED = "failed"
    """Rendering or playback broke. Survivable, and invisible to the player."""


@dataclass(frozen=True, slots=True)
class Utterance:
    """One thing to say, and how much it matters.

    `voice` is a deliberate hole in the abstraction, and a small one: it is
    `None` for everything CaddieJ currently says, meaning "whatever voice is
    configured". It exists so that a second speaker — a different character for
    warnings, say — does not need a second service.
    """

    text: str
    priority: SpeechPriority = SpeechPriority.COACHING
    utterance_id: UUID = field(default_factory=uuid4)
    shot_id: UUID | None = None
    """The shot this is about, when it is about one. Carried so that a listener
    can tie a spoken line to what is on the floor, and so a line for a swing
    three ago can be recognised as one."""

    voice: str | None = None
    perishable: bool = True
    """Whether a newer utterance of the same priority makes this one pointless.

    True for coaching, where being late is the same as being wrong. False for
    anything where two messages are two messages."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """How loud a rendered line is, over time, between nothing and its peak.

    A shape rather than a sound. Sixty-odd numbers describe a three-second
    sentence closely enough to drive a light, which is three orders of magnitude
    smaller than the samples they came from — small enough to put on an event,
    where anything that wants to react to a voice can read it without ever
    touching audio.

    Normalised against its own peak, deliberately. The interesting thing is the
    shape of the words, not the volume the engine happened to produce, and a
    listener that had to cope with both would need to know what a loud utterance
    sounds like. Absolute level is the sound card's business.
    """

    values: tuple[float, ...]
    frame_rate: float
    """Samples per second. Low — tens of hertz, not thousands. The syllables are
    the signal, so anything faster is detail nobody can see in a light."""

    def __post_init__(self) -> None:
        if self.frame_rate <= 0:
            raise ValueError("frame_rate must be positive")
        if any(not 0.0 <= value <= 1.0 for value in self.values):
            raise ValueError("envelope values must lie between zero and one")

    @property
    def duration_seconds(self) -> float:
        return len(self.values) / self.frame_rate

    @property
    def is_empty(self) -> bool:
        return not self.values

    @property
    def peak(self) -> float:
        return max(self.values, default=0.0)

    def at(self, seconds: float) -> float:
        """The level at a moment, interpolated between the two frames around it.

        Interpolated because whoever is reading this samples it on their own
        clock — a light with a command budget of a few per second, say — and
        that clock has no reason to align with the analysis. Stepping to the
        nearest frame instead would turn a smooth line into a staircase whose
        steps are visible precisely when the reader is slowest.

        Outside the utterance the answer is zero: before it there is nothing
        yet, and after it there is nothing left.
        """

        if not self.values or seconds < 0.0:
            return 0.0

        position = seconds * self.frame_rate
        index = math.floor(position)
        if index >= len(self.values) - 1:
            return self.values[-1] if index == len(self.values) - 1 else 0.0

        rise = self.values[index + 1] - self.values[index]
        return self.values[index] + rise * (position - index)
