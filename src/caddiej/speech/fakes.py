"""A voice and a speaker that exist only inside the process.

Everything the real subsystem has to survive can be arranged here: a slow
engine, an engine that breaks on one sentence, an engine that never returns, a
sound card that fails mid-word, and a line that has to be cut off exactly
halfway through. None of it needs a model, a download, or a speaker in the room,
and the whole thing runs in a test in milliseconds.

`RecordingSink` is the interesting one. It records what it *started* separately
from what it *finished*, because that is the difference an interruption makes,
and it can be held open on a gate so a test can decide precisely when a sentence
is halfway through rather than sleeping and hoping.

Together these also give CaddieJ a demo mode: set the engine to `fake` and the
whole path from a coaching line to `SpeechCompleted` runs with nothing
installed. The fake voice babbles rather than staying silent for that reason —
see `babble` below.
"""

import asyncio
import math
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field

from caddiej.speech.audio import DEFAULT_SAMPLE_RATE, Audio, silence
from caddiej.speech.sink import PlaybackError
from caddiej.speech.synth import SynthesisFailedError

# Roughly a spoken word every fifth of a second, which is close enough to a real
# voice that a test about timing reads as if it were about speech.
SECONDS_PER_CHARACTER = 0.05

# What the fake voice sounds like: bursts about as long as a syllable, with a
# short gap after each. The numbers matter only in that the result has a shape a
# measurement can find, which is what the lights react to.
SYLLABLE_SECONDS = 0.19
GAP_SECONDS = 0.07
BABBLE_HERTZ = 190.0
# Quiet. This is never meant to be listened to, but `SPEECH_OUTPUT=sounddevice`
# with the fake engine will happily play it, and a full-scale tone arriving
# unannounced in somebody's room is a poor way to find that out.
BABBLE_AMPLITUDE = 0.12


def babble(seconds: float, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Audio:
    """A quiet, wordless thing with the rhythm of speech.

    Not silence, and that distinction earns its keep: an envelope measured from
    silence has no shape in it, so a fake engine that returned silence would
    quietly disable everything downstream that reacts to the voice. Anything
    testing the lights, or watching them in a demo, would then be testing
    nothing at all.

    Each syllable rises and falls under a raised cosine, so what a measurement
    finds is a run of humps with gaps between them — which is, at the level of
    detail a lamp can express, what a sentence looks like.
    """

    frames = max(0, round(seconds * sample_rate))
    period = SYLLABLE_SECONDS + GAP_SECONDS
    samples = array("h", bytes(frames * 2))
    for index in range(frames):
        at = (index / sample_rate) % period
        if at >= SYLLABLE_SECONDS:
            continue
        shape = 0.5 - 0.5 * math.cos(2 * math.pi * at / SYLLABLE_SECONDS)
        carrier = math.sin(2 * math.pi * BABBLE_HERTZ * index / sample_rate)
        samples[index] = round(BABBLE_AMPLITUDE * shape * carrier * 32767)
    return Audio(data=samples.tobytes(), sample_rate=sample_rate)


@dataclass
class FakeSynthesizer:
    """Returns wordless babble of a believable length instead of a voice."""

    voice_name: str = "bm_george"
    configured: bool = True
    open_error: BaseException | None = None
    error: BaseException | None = None
    delay: float = 0.0
    """How long rendering takes. Longer than the service's render timeout is
    how a hung engine is tested."""

    open_delay: float = 0.0
    """How long loading the voice takes. Longer than the service's open timeout
    is how a first run downloading a model on bad wireless is tested."""

    seconds_per_character: float = SECONDS_PER_CHARACTER
    rate: int = DEFAULT_SAMPLE_RATE
    calls: list[tuple[str, str]] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def voice(self) -> str:
        return self.voice_name

    @property
    def sample_rate(self) -> int:
        return self.rate

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def open(self) -> None:
        self.opened += 1
        if self.open_delay:
            await asyncio.sleep(self.open_delay)
        if self.open_error is not None:
            raise self.open_error

    async def render(self, text: str, voice: str | None = None) -> Audio:
        self.calls.append((text, voice or self.voice_name))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return babble(len(text) * self.seconds_per_character, self.rate)

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class ScriptedSynthesizer:
    """Answers with the next thing on the script, then repeats the last one.

    Repeating rather than running out, so a test about the third utterance does
    not have to say anything about the first two.
    """

    replies: Sequence[Audio | BaseException] = ()
    voice_name: str = "bm_george"
    calls: list[str] = field(default_factory=list)
    _index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.replies:
            self.replies = (silence(0.5),)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def voice(self) -> str:
        return self.voice_name

    @property
    def sample_rate(self) -> int:
        return DEFAULT_SAMPLE_RATE

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def render(self, text: str, voice: str | None = None) -> Audio:
        self.calls.append(text)
        reply = self.replies[min(self._index, len(self.replies) - 1)]
        self._index += 1
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def aclose(self) -> None:
        return None


@dataclass
class BrokenSynthesizer:
    """Fails the same way every time, on every sentence."""

    error: BaseException = field(
        default_factory=lambda: SynthesisFailedError("the voice broke")
    )
    voice_name: str = "bm_george"
    calls: int = 0

    @property
    def name(self) -> str:
        return "broken"

    @property
    def voice(self) -> str:
        return self.voice_name

    @property
    def sample_rate(self) -> int:
        return DEFAULT_SAMPLE_RATE

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def render(self, text: str, voice: str | None = None) -> Audio:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None


@dataclass
class RecordingSink:
    """A speaker that remembers, and that can be stopped mid-sentence.

    `started` and `played` are separate on purpose: an interrupted line appears
    in the first and never in the second, which is the assertion an interruption
    test actually wants to make.
    """

    realtime: bool = False
    """Whether to take as long as the audio really runs for. Off by default so
    that tests about queueing do not also become tests about the clock."""

    speed: float = 1.0
    """Divides the wait when `realtime` is set, so a two-second sentence can be
    played in twenty milliseconds without pretending it is a short one."""

    gate: asyncio.Event | None = None
    """When set, playing blocks until the gate is opened. This is how a test
    puts a sentence exactly halfway through rather than sleeping and hoping."""

    error: BaseException | None = None
    configured: bool = True
    open_error: BaseException | None = None

    started: list[Audio] = field(default_factory=list)
    played: list[Audio] = field(default_factory=list)
    interrupted: int = 0
    opened: int = 0
    closed: int = 0

    @property
    def name(self) -> str:
        return "recording"

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def open(self) -> None:
        self.opened += 1
        if self.open_error is not None:
            raise self.open_error

    async def play(self, audio: Audio) -> None:
        self.started.append(audio)
        if self.error is not None:
            raise self.error
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.realtime:
                await asyncio.sleep(audio.duration_seconds / self.speed)
        except asyncio.CancelledError:
            # A real sink silences the device here. Counting it is the fake's
            # equivalent, and it is what proves the tail of an interrupted
            # sentence never reaches the room.
            self.interrupted += 1
            raise
        self.played.append(audio)

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class BrokenSink:
    """Fails once the audio is in hand, which is where a real one usually does."""

    error: BaseException = field(
        default_factory=lambda: PlaybackError("the speaker went away")
    )
    calls: int = 0

    @property
    def name(self) -> str:
        return "broken"

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def play(self, audio: Audio) -> None:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None
