"""Something that can turn a sentence into sound.

The interface is one method wide on purpose. Text in, samples out, no streaming,
no callbacks, no voice enumeration, no model handles. Everything a particular
engine believes about itself — that it wants a language code, that it yields
chunks, that its output is a tensor on a device, that it needs a phonemiser
installed — is that engine's adapter's problem, and stops there.

Rendering is allowed to be slow. It happens on a worker, off the event path, and
the caller imposes the timeout, so an engine may take as long as it takes and
must not invent its own deadline.

Rendering is **not** allowed to block the event loop. A local engine is a lump
of CPU work and an adapter that runs it inline would freeze the floor, the
launch monitor and the HUD for the length of a sentence, so every implementation
hands the work to a thread.

The errors below are the three the service reacts to differently, and no more.
"""

from typing import Protocol

from caddiej.speech.audio import Audio


class SynthesisError(RuntimeError):
    """Speech could not be rendered. Never reaches the player as anything but
    silence."""


class SynthesisUnavailableError(SynthesisError):
    """The engine cannot work at all: no package, no model, no weights.

    Distinct from a failure because the answer does not change by trying again.
    Raised at start-up, where it switches speech off with one log line rather
    than failing once per shot for an evening.
    """


class SynthesisFailedError(SynthesisError):
    """This text could not be rendered, though the engine is fine."""


class Synthesizer(Protocol):
    """An engine that can speak one line."""

    @property
    def name(self) -> str:
        """Which engine this is, for logs and for health."""

    @property
    def voice(self) -> str:
        """The voice used when an utterance does not name one."""

    @property
    def sample_rate(self) -> int:
        """What this engine produces, so a sink can be told before it runs."""

    @property
    def is_configured(self) -> bool:
        """Whether this could work at all. Cheap: no model loading, no I/O."""

    async def open(self) -> None:
        """Get ready to speak, however long that takes.

        Kept separate from rendering because a local engine loads hundreds of
        megabytes the first time it is used, and paying that on the first
        coaching line of an evening would mean the line arrives after the next
        shot. Raises `SynthesisUnavailableError` if it cannot get ready.
        """

    async def render(self, text: str, voice: str | None = None) -> Audio:
        """Speak the text, raising a `SynthesisError` if it cannot."""

    async def aclose(self) -> None:
        """Let go of the model and anything holding it. Called once."""
