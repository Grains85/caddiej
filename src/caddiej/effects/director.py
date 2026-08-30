"""Deciding what the room is doing, and making sure it stops doing it.

Everything in this file exists to keep one promise: **at any moment there is at
most one effect, and when it is over every output is back to normal.** A
celebration subsystem that cannot promise that is worse than no celebration
subsystem, because the failure mode is a projector stuck showing BIRDIE over
somebody's next forty shots.

Five rules, and the last one is the one that took the thought.

**The event path never waits.** A handler starts a task and returns. The bus
dispatches handlers one after another on a single worker, so a handler that
stayed for the length of a celebration would hold up every shot behind it — and
the word on the floor is the least important thing on that queue.

**One effect at a time, and it is a task rather than a queue.** Effects are about
moments. An eagle rendered forty seconds late, because a queue was draining, is
not a late celebration; it is a wrong one, arriving over the top of whatever the
player is doing now. So there is no queue: an effect either happens now or does
not happen.

**Higher priority replaces; equal or lower is refused.** Replacement rather than
queueing for the same reason as above. Refusal rather than replacement for equal
priority because the running effect is already on screen, and swapping one
celebration for an equally important one half way through reads as a bug. The
first one wins and the second is counted.

**Everything is capped.** The duration is clamped, so a catalogue entry with a
typo in it cannot hold the room for a minute. Every call into a channel is
wrapped in a timeout, so an OBS that accepts a connection and then stops
answering cannot hold an effect open. There is no path by which a runaway effect
outlives its own cap.

**The reset is unconditional, and it is somebody else's task.** Every channel is
reset at the end of every effect — including channels the effect never touched
and channels whose `begin` failed — because remembering which ones were touched
is bookkeeping that only has to be wrong once, in the replacement case, to leave
a source visible forever. And the reset runs on its own task rather than in the
effect's, because effects are cancelled routinely and a reset abandoned half way
is the exact failure this whole file is about. Replacement is the one case with
no reset at all, and that is correct: channels take absolute values, so the
incoming effect overwrites the outgoing one on every output, and resetting in
between would only produce a visible flicker.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from caddiej.core.event_bus import EventBus
from caddiej.domain.scoring import HoleCompleted, HoleResult
from caddiej.effects.catalogue import effect_for
from caddiej.effects.channel import EffectChannel
from caddiej.effects.effect import Effect

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EffectsHealth:
    """What the room has celebrated, and everything that went wrong doing it.

    `reset_failures` is separate from `failures` for the same reason lighting
    separates its restores: a failed `begin` means one celebration was quieter
    than intended, which nobody will notice, while a failed `end` means an output
    is still showing something and will keep showing it. Those are not the same
    severity and should not share a number.
    """

    enabled: bool
    available: bool
    channels: tuple[str, ...]
    playing: str | None
    plays: int
    refused: int
    replaced: int
    resets: int
    failures: int
    reset_failures: int
    last_error: str | None


class EffectDirector:
    """Owns which effect is running, for how long, and what undoes it."""

    def __init__(
        self,
        channels: Sequence[EffectChannel] = (),
        enabled: bool = True,
        max_seconds: float = 8.0,
        channel_timeout_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if channel_timeout_seconds <= 0:
            raise ValueError("channel_timeout_seconds must be positive")

        self._channels = tuple(channels)
        self._enabled = enabled
        self._max_seconds = max_seconds
        self._channel_timeout = channel_timeout_seconds
        self._monotonic = monotonic

        self._playing: Effect | None = None
        self._running: asyncio.Task[None] | None = None
        # Resetting is its own task, never part of an effect, so that cancelling
        # an effect — which is how replacement and shutdown both work — can never
        # abandon a reset. Nothing cancels this one.
        self._resetting: asyncio.Task[None] | None = None
        self._reset_lock = asyncio.Lock()
        self._generation = 0
        # Whatever the channels may currently be holding, which is not the same
        # question as what is playing: an effect stops playing the instant its
        # duration runs out, and the outputs are still showing it until the
        # reset lands. Kept separately so the reset knows what it is undoing
        # after `_playing` has already been cleared.
        self._held: Effect | None = None

        self.plays = 0
        self.refused = 0
        self.replaced = 0
        self.resets = 0
        self.failures = 0
        self.reset_failures = 0
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether an effect now would reach anything at all."""

        return self._enabled and any(channel.is_available for channel in self._channels)

    @property
    def is_playing(self) -> bool:
        return self._running is not None and not self._running.done()

    @property
    def is_idle(self) -> bool:
        """Nothing running, and nothing left showing that has not been reset."""

        return (
            not self.is_playing
            and self._held is None
            and (self._resetting is None or self._resetting.done())
        )

    async def aclose(self) -> None:
        """Stop celebrating, put every output back, then let the channels go.

        The reset is why this cannot be a cancel-and-forget. Whatever is mid-
        effect is abandoned at once — nobody wants to wait for a light show on the
        way out of a process — but the room is normal again before this returns,
        because after it nothing is left running that knows how to make it so.
        """

        await self._stop()
        self._reset_soon()
        await self._settle()
        for channel in self._channels:
            try:
                await channel.aclose()
            except Exception:
                logger.warning(
                    "effects_channel_close_failed",
                    extra={"effect_channel": channel.name},
                    exc_info=True,
                )

    # --- the event path ----------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(HoleCompleted, self.on_hole_completed, name="effects")

    async def on_hole_completed(self, event: HoleCompleted) -> None:
        """React to a finished hole, if the catalogue says how.

        Nothing publishes this yet; see `caddiej.domain.scoring`. It is
        subscribed anyway so that scoring, when it exists, needs no change
        here — which is the whole reason a hook is worth having before the thing
        that fires it.
        """

        await self.play(event.result)

    async def play(self, result: HoleResult) -> bool:
        """Run the effect for a result. Returns whether it started.

        Public, and not only for the handler: this is what a future scoring
        module, a manual trigger or a test calls, and the name of what happened
        is the whole of what any of them needs to know about floors, lights,
        scenes and voices.
        """

        effect = effect_for(result)
        if effect is None:
            logger.warning("effects_undefined", extra={"effect_cue": str(result)})
            return False
        return self.start(effect)

    def start(self, effect: Effect) -> bool:
        """Begin an effect, replacing or refusing whatever is running.

        Synchronous, and that is the point: it is called from a bus handler, and
        everything slow about an effect happens on the task it creates.
        """

        if not self._enabled or not self._channels:
            return False

        running = self._running
        if running is not None and not running.done():
            current = self._playing
            if current is not None and effect.priority <= current.priority:
                self.refused += 1
                logger.info(
                    "effects_refused",
                    extra={
                        "effect_cue": effect.cue,
                        "effect_playing": current.cue,
                        "effect_priority": int(effect.priority),
                    },
                )
                return False

            self.replaced += 1
            logger.info(
                "effects_replaced",
                extra={
                    "effect_cue": effect.cue,
                    "effect_playing": "" if current is None else current.cue,
                },
            )
            # Not awaited: this runs on the bus worker. The cancelled task sees
            # that a newer generation exists and resets nothing, leaving the
            # incoming effect to overwrite every output.
            running.cancel()

        self._generation += 1
        self._playing = effect
        # Marked as held before anything has actually begun, deliberately. A
        # reset that runs when nothing was showing costs a few calls that do
        # nothing; an effect cancelled part way through starting its channels
        # with nothing recorded would leave whichever ones had already begun
        # showing forever. One of those is cheap and the other is the bug.
        self._held = effect
        self.plays += 1
        self._running = asyncio.create_task(
            self._run(effect, self._generation), name=f"effect-{effect.cue}"
        )
        return True

    # --- running an effect -------------------------------------------------

    async def _run(self, effect: Effect, generation: int) -> None:
        """Begin every channel, hold for the duration, then hand back the room."""

        seconds = min(effect.seconds, self._max_seconds)
        started = self._monotonic()
        try:
            for channel in self._channels:
                if channel.is_available:
                    await self._call(channel.begin, channel, effect, "begin")
            logger.info(
                "effects_started",
                extra={
                    "effect_cue": effect.cue,
                    "effect_priority": int(effect.priority),
                    "effect_seconds": round(seconds, 2),
                },
            )
            # Measured rather than a bare sleep, so the time channels spent
            # being told about the effect comes out of the effect rather than
            # being added to it. An OBS that takes half a second to answer
            # should shorten the celebration, not lengthen it past the label's
            # own animation.
            remaining = seconds - (self._monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            # Expected: this is how replacement and shutdown both stop an
            # effect.
            raise
        finally:
            # Conditional on still being the current effect. A task that has
            # been superseded owns nothing, and resetting from here would undo
            # the work of whatever replaced it.
            if self._generation == generation:
                self._playing = None
                self._running = None
                self._reset_soon()

    async def _call(
        self,
        method: Callable[[Effect], Awaitable[None]],
        channel: EffectChannel,
        effect: Effect,
        phase: str,
    ) -> bool:
        """One call into one channel. Returns whether it worked.

        Isolated per channel and per call, because the channels are independent
        outputs and a bridge that has gone away is no reason for the floor to
        stay blank. The timeout is the part that matters: a channel that accepts
        a command and never answers would otherwise hold an effect open for as
        long as it liked, which is exactly the runaway this class exists to
        prevent.
        """

        try:
            async with asyncio.timeout(self._channel_timeout):
                await method(effect)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if phase == "begin":
                self.failures += 1
            else:
                self.reset_failures += 1
            self.last_error = f"{channel.name}: {type(error).__name__}: {error}"[:300]
            logger.warning(
                "effects_channel_failed",
                extra={
                    "effect_cue": effect.cue,
                    "effect_channel": channel.name,
                    "effect_phase": phase,
                },
                exc_info=error,
            )
            return False
        return True

    # --- giving the room back ----------------------------------------------

    def _reset_soon(self) -> None:
        """Start putting the outputs back, without making the caller wait.

        Resetting reaches a projector's clients and possibly a program on
        another machine, and the handler that asks for it is running on the bus
        worker with shots queued behind it. So it becomes a task — but its own
        task, for the reason in this module's note.
        """

        if self._held is None:
            return
        # Tagged with the generation it is undoing. Without that, a reset still
        # in flight when the next effect starts would reset the *new* effect's
        # outputs a moment after they were set — a rare ordering, and one that
        # would present as a celebration that silently did nothing.
        self._resetting = asyncio.create_task(
            self._reset(self._generation), name="effect-reset"
        )

    async def _settle(self) -> None:
        """Wait for a reset in flight, so shutdown cannot outrun the room."""

        resetting = self._resetting
        self._resetting = None
        if resetting is not None and not resetting.done():
            await asyncio.gather(resetting, return_exceptions=True)

    async def _reset(self, generation: int) -> None:
        """Put every channel back to normal, whatever it was doing.

        Under a lock and re-checked inside it, so two callers arriving at once —
        an effect running out at the same moment shutdown is asked for — reset
        once rather than twice.

        Every channel, not only the available ones. A channel that has become
        unavailable since the effect began is precisely the channel most likely
        to be left showing something, and `end` is required to be safe on a
        channel that never began.
        """

        async with self._reset_lock:
            if self._generation != generation:
                # A newer effect took the outputs while this was waiting. They
                # are its to give back, and it will, when it ends.
                return

            effect = self._held
            if effect is None:
                return

            for channel in self._channels:
                await self._call(channel.end, channel, effect, "end")

            # Cleared even when a channel failed. Keeping it would mean trying
            # the same failing command again on every future effect, and an
            # output nobody can reach is not made more reachable by asking twice.
            self._held = None
            self.resets += 1
            logger.info("effects_reset", extra={"effect_cue": effect.cue})

    async def _stop(self) -> None:
        """Cancel the running effect, if any, and wait for it to unwind.

        Only the effect. A reset in flight is left strictly alone; `_settle` is
        how anything waits for that.
        """

        running = self._running
        self._running = None
        if running is None or running.done():
            return
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    # --- reporting ---------------------------------------------------------

    def health(self) -> EffectsHealth:
        return EffectsHealth(
            enabled=self._enabled,
            available=self.is_available,
            channels=tuple(
                channel.name for channel in self._channels if channel.is_available
            ),
            playing=None if self._playing is None else self._playing.cue,
            plays=self.plays,
            refused=self.refused,
            replaced=self.replaced,
            resets=self.resets,
            failures=self.failures,
            reset_failures=self.reset_failures,
            last_error=self.last_error,
        )
