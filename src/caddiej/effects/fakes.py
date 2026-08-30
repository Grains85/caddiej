"""Channels that exist only inside the process.

Everything the director has to survive can be arranged here: a channel that
fails on the way in, a channel that fails on the way *out* — which is the
interesting one, because a failed reset is the only failure anybody sees — a
channel that hangs, and a channel that is simply not there.

`RecordingChannel` keeps `begun` and `ended` as separate lists rather than one
log of calls, because nearly every assertion about this subsystem is of the form
"this started and was definitely put back", and two lists make that a comparison
rather than a search.
"""

import asyncio
from dataclasses import dataclass, field

from caddiej.effects.effect import Effect


class ChannelError(Exception):
    """What a channel raises when its output cannot be reached. Test scenery."""


@dataclass
class RecordingChannel:
    """Remembers what it was told, and can be made to fail at either end."""

    channel_name: str = "recording"
    available: bool = True

    begin_error: BaseException | None = None
    end_error: BaseException | None = None

    begin_delay: float = 0.0
    """How long starting takes. Longer than the director's channel timeout is how
    a hung output is tested."""

    end_delay: float = 0.0
    """How long resetting takes. The more interesting of the two: an output that
    accepts a command and then stops answering on the way *out* is the case where
    something stays on screen."""

    gate: asyncio.Event | None = None
    """When set, `begin` blocks until the gate is opened. This is how a test holds
    an effect exactly part way through starting instead of sleeping and hoping."""

    begun: list[Effect] = field(default_factory=list)
    ended: list[Effect] = field(default_factory=list)
    closed: int = 0

    @property
    def name(self) -> str:
        return self.channel_name

    @property
    def is_available(self) -> bool:
        return self.available

    @property
    def cues(self) -> list[str]:
        """The cue of everything begun, in order. A convenience for the common
        assertion, which is about which effects ran rather than their contents."""

        return [effect.cue for effect in self.begun]

    @property
    def is_showing(self) -> bool:
        """Whether more has been begun than ended — the leak this all guards."""

        return len(self.begun) > len(self.ended)

    async def begin(self, effect: Effect) -> None:
        self.begun.append(effect)
        if self.gate is not None:
            await self.gate.wait()
        if self.begin_delay:
            await asyncio.sleep(self.begin_delay)
        if self.begin_error is not None:
            raise self.begin_error

    async def end(self, effect: Effect) -> None:
        self.ended.append(effect)
        if self.end_delay:
            await asyncio.sleep(self.end_delay)
        if self.end_error is not None:
            raise self.end_error

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class BrokenChannel:
    """Fails everything the same way, both directions.

    What an unplugged output looks like from here, and the thing that proves a
    dead one costs a counter rather than a celebration.
    """

    error: BaseException = field(
        default_factory=lambda: ChannelError("the output did not answer")
    )
    channel_name: str = "broken"
    calls: int = 0

    @property
    def name(self) -> str:
        return self.channel_name

    @property
    def is_available(self) -> bool:
        return True

    async def begin(self, effect: Effect) -> None:
        self.calls += 1
        raise self.error

    async def end(self, effect: Effect) -> None:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None
