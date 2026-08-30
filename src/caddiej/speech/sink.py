"""Somewhere for the sound to go.

Separate from the engine, and not merely for tidiness: rendering and playing
fail for unrelated reasons, at unrelated times, and only one of them can be
usefully cached. Splitting them is also what makes "render but stay silent" a
configuration rather than a special case, which is how the whole pipeline gets
exercised on a build machine with no sound card.

**Playing is cancellable, and that is the whole interruption mechanism.** There
is no `stop`. A sink is played inside a task, and cutting a sentence short is
cancelling that task, which means the one thing every async caller already knows
how to do is also the thing that works here. An implementation must therefore
silence the device on the way out of a `CancelledError` rather than letting the
tail of a sentence keep playing after the caller has moved on.

**Playing returns when the sound has finished, not when it has been handed
over.** Everything upstream depends on that: it is what makes "one voice at a
time" true by construction, and it is what makes `SpeechCompleted` a fact about
the room rather than about a buffer.
"""

from typing import Protocol

from caddiej.speech.audio import Audio


class PlaybackError(RuntimeError):
    """The audio could not be played."""


class PlaybackUnavailableError(PlaybackError):
    """There is no usable output: no package, no device, no permission.

    Raised at start-up. Speech then renders nothing and says so in health,
    rather than failing once per sentence.
    """


class AudioSink(Protocol):
    """An output that plays one utterance at a time, to completion."""

    @property
    def name(self) -> str: ...

    @property
    def is_configured(self) -> bool:
        """Whether this could play anything. Cheap: no device is opened."""

    async def open(self) -> None:
        """Claim the output device. Raises `PlaybackUnavailableError`."""

    async def play(self, audio: Audio) -> None:
        """Play it all, then return.

        Raises `PlaybackError` if the device fails mid-sentence. Must stop the
        sound before propagating `CancelledError`: a cancelled playback that
        keeps making noise would let two voices overlap, which is the one thing
        the service above promises cannot happen.
        """

    async def aclose(self) -> None:
        """Release the device. Called once, at shutdown."""


class NullSink:
    """Renders everything, plays nothing, takes no time.

    A real output rather than a test double. It is what `SPEECH_OUTPUT=none`
    selects, and it earns its place twice: on a machine with no sound card the
    whole pipeline — queue, engine, cache, events — still runs and can be
    watched, and somebody who wants the speech events without the noise now has
    a supported way to ask for that instead of a workaround.
    """

    @property
    def name(self) -> str:
        return "none"

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def play(self, audio: Audio) -> None:
        return None

    async def aclose(self) -> None:
        return None
