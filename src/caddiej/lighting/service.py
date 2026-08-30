"""Making the room move with the voice, and putting it back afterwards.

The shape of this file is the same as speech's and coaching's, because the
problem is the same: something slow and fallible on a home network has to happen
near something that must never be delayed. What is different is that lighting
touches something it does not own. The lamps in somebody's living room were set
the way they are on purpose, so the whole subsystem is built around one promise —
**it gives the room back** — and most of the care here is spent on the paths
where that is hard rather than on the paths where it is easy.

Five rules.

**The event path never waits.** A handler measures nothing, sends nothing and
awaits nothing that can block. It starts a task and returns. The bus dispatches
handlers one after another on a single worker, so a handler that stayed for the
length of a sentence would hold up every shot behind it — and the light being
dimmed is the least important thing on that queue.

**One reaction at a time.** A single task, replaced rather than joined. Two
things fading the same lamp in opposite directions is not a race worth managing,
so it is one this file cannot express.

**The baseline is captured once and held.** Whatever the lights were doing when
CaddieJ started talking is the reference for everything that follows, and it is
read exactly once and kept until the room is idle again. Re-reading it during a
reaction would measure CaddieJ's own dimming and ratchet the room down a little
with every sentence, which is the single worst bug this subsystem could have.

**Commands are budgeted, and the bridge does the smoothing.** A Hue bridge
tolerates something like ten commands a second in total, so the tick is derived
from that budget and the number of lights rather than from what would look best.
Each command asks for a transition as long as the gap to the next one, which
turns five updates a second into something continuous. Sending more commands to
draw the same curve would produce something a bridge refuses.

**Nothing here can affect anything else.** Not the voice, not a shot, not the
floor. Every failure ends in a counter and a log line; a bridge that has stopped
answering is left alone for a while rather than retried once per syllable; and
the last thing tried, always, is giving the room back.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from caddiej.core.event_bus import EventBus
from caddiej.domain.events import SpeechCompleted, SpeechStarted
from caddiej.lighting.modulation import (
    Curve,
    Flourish,
    Modulation,
    speaking_curve,
)
from caddiej.lighting.port import (
    LightingCommandError,
    LightingPort,
    LightingUnavailableError,
    LightState,
    NullLights,
)

logger = logging.getLogger(__name__)

# Zero is not a brightness — to most vendors it means off, and turning somebody's
# lamp off is not a subtle reaction. One per cent is the floor instead, which is
# the dimmest thing a Hue light will hold without switching itself out.
MIN_BRIGHTNESS = 0.01

# How much a light has to want to move before a command is worth spending. Two
# per cent of full brightness is below what anybody can see as a step, so this
# costs nothing visually and removes most of the traffic during the quiet parts
# of a sentence, where the curve is flat and the budget is better saved for the
# parts where it is not.
DEADBAND = 0.02

# The fastest the tick is ever allowed to run, whatever the arithmetic says.
# A guard against a configuration that would flood a bridge, not a tuning knob.
MIN_INTERVAL_SECONDS = 0.08

# How many times to try giving the room back. Two, not more: see `_give_back`.
# The asymmetry is the whole argument — a wasted attempt costs one timeout that
# nobody is waiting for, and a missing attempt costs somebody their lighting.
_RESTORE_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class LightingHealth:
    """What the room has been doing, and everything that stopped it.

    `restore_failures` is the number that matters, and it is separate from
    `failures` for that reason: a dropped modulation command is invisible, while
    a dropped restore means somebody's living room is a little dimmer than they
    left it and nothing in the house is going to fix it.
    """

    enabled: bool
    available: bool
    output: str | None
    lights: int
    reacting: bool
    updates_per_second: float
    reactions: int
    cues: int
    commands: int
    coalesced: int
    restores: int
    failures: int
    restore_failures: int
    suspended: bool
    last_error: str | None


class LightingService:
    """Owns the lights, the tick, and the promise to give the room back."""

    def __init__(
        self,
        lights: LightingPort | None = None,
        modulation: Modulation | None = None,
        enabled: bool = True,
        max_commands_per_second: float = 10.0,
        transition_scale: float = 1.0,
        failure_limit: int = 3,
        cooldown_seconds: float = 60.0,
        command_timeout_seconds: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_commands_per_second <= 0:
            raise ValueError("max_commands_per_second must be positive")
        if failure_limit < 1:
            raise ValueError("failure_limit must allow at least one failure")

        self._lights = NullLights() if lights is None else lights
        self._modulation = Modulation() if modulation is None else modulation
        self._enabled = enabled
        self._budget = max_commands_per_second
        self._transition_scale = transition_scale
        self._failure_limit = failure_limit
        self._cooldown = cooldown_seconds
        self._command_timeout = command_timeout_seconds
        self._monotonic = monotonic

        self._ready = False
        self._baseline: tuple[LightState, ...] | None = None
        self._dimmable: tuple[LightState, ...] = ()
        self._reaction: asyncio.Task[None] | None = None
        # Giving the room back is its own task, never part of a reaction, so
        # that cancelling a reaction — which happens constantly — can never
        # interrupt a restore. Nothing cancels this one but the end of the world.
        self._restoring: asyncio.Task[None] | None = None
        self._restore_lock = asyncio.Lock()
        self._generation = 0
        self._utterance: UUID | None = None
        self._cueing = False
        self._consecutive_failures = 0
        self._quiet_until = 0.0
        self._interval = MIN_INTERVAL_SECONDS

        self.reactions = 0
        self.cues = 0
        self.commands = 0
        self.coalesced = 0
        self.restores = 0
        self.failures = 0
        self.restore_failures = 0
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether a reaction now would actually reach a lamp."""

        return self._ready and self._enabled and not self.is_suspended

    @property
    def is_suspended(self) -> bool:
        """Whether the bridge is being left alone after repeated failures."""

        return self._monotonic() < self._quiet_until

    @property
    def is_reacting(self) -> bool:
        return self._reaction is not None and not self._reaction.done()

    @property
    def is_idle(self) -> bool:
        """Nothing reacting, and nothing held that has not been given back."""

        return (
            not self.is_reacting
            and self._baseline is None
            and (self._restoring is None or self._restoring.done())
        )

    async def open(self) -> None:
        """Reach the lights and work out how fast they may be driven.

        Never raises. A missing bridge, a revoked key or a room with no dimmable
        lamp in it are all supported ways to run CaddieJ, and none of them is a
        reason for the rest of it not to start.
        """

        if not self._enabled:
            logger.info("lighting_disabled")
            return
        if not self._lights.is_configured:
            logger.warning(
                "lighting_unconfigured", extra={"lighting_output": self._lights.name}
            )
            return

        try:
            await self._lights.open()
        except LightingUnavailableError as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning("lighting_unavailable", exc_info=error)
            return
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.exception("lighting_open_failed")
            return

        self._ready = True
        logger.info(
            "lighting_ready",
            extra={
                "lighting_output": self._lights.name,
                "lighting_minimum": self._modulation.minimum,
                "lighting_maximum": self._modulation.maximum,
            },
        )

    async def aclose(self) -> None:
        """Stop reacting and give the room back.

        The restore is the reason this cannot be a cancel-and-forget. Whatever is
        mid-dip is abandoned immediately — nobody wants to wait for a light show
        on the way out of a process — but the room is put back before the method
        returns, because the alternative is leaving somebody's living room dark
        with nothing left running that knows how to fix it.
        """

        await self._stop()
        self._restore_soon()
        await self._settle()
        try:
            await self._lights.aclose()
        except Exception:
            logger.warning("lighting_close_failed", exc_info=True)
        self._ready = False

    # --- the event path ----------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        """Follow the voice. Nothing else.

        Reactions to things that happen in a round arrive through `play`, called
        by whatever is orchestrating them, rather than by this service reading
        the bus for itself. That is the difference between lighting being one
        output among several and lighting being a second, quieter place where
        the room's response to a birdie is decided.
        """

        bus.subscribe(SpeechStarted, self.on_speech_started, name="lighting")
        bus.subscribe(SpeechCompleted, self.on_speech_completed, name="lighting")

    async def on_speech_started(self, event: SpeechStarted) -> None:
        """Follow a line that has just started playing.

        Driven by the measured envelope on the event and nothing else. An engine
        that reported no envelope, or reported silence, gets no reaction rather
        than an invented one: a room pulsing to a shape nobody measured would
        eventually disagree with what somebody can hear, and a light that
        disagrees with the audio is worse than a light that does nothing.
        """

        envelope = event.envelope
        if envelope is None or envelope.is_empty:
            return
        if self._modulation.is_flat:
            return
        if self._cueing:
            # A celebration is playing. It is rarer and more interesting than
            # anything being said over the top of it, so it keeps the room.
            return

        self._utterance = event.utterance_id
        self._begin(
            speaking_curve(envelope, self._modulation),
            seconds=event.duration_seconds,
            reason="speech",
            cue=False,
        )

    async def on_speech_completed(self, event: SpeechCompleted) -> None:
        """Give the room back when the line stops, however it stopped.

        Needed as well as the curve running out, because a sentence can be cut
        short: an interrupted line leaves the reaction part-way through a dip,
        and a room that stayed there until the next shot would look broken.

        Only for the line actually being followed. Most `SpeechCompleted` events
        describe utterances that never made a sound, and treating one of those as
        a reason to restore would cancel the reaction to the line that is playing.
        """

        if self._utterance != event.utterance_id:
            return
        self._utterance = None
        await self._stop()
        self._restore_soon()

    async def play(self, flourish: Flourish, reason: str = "cue") -> bool:
        """Run one gesture. Returns whether it started.

        The only way into this subsystem other than the voice, and a gesture is
        the whole of what a caller has to express: how deep and how long. What a
        birdie is worth in those terms is decided in `caddiej.effects`, where the
        same judgement also decides what the floor and the voice do about it —
        which is the point of having one place for it.

        `reason` names the caller in the logs and in the task name. It changes
        nothing about the light.
        """

        # A cue outranks a sentence, so whatever is running loses the room. The
        # baseline is deliberately not restored in between: it is held across the
        # handover, so the lights move from where they are into the gesture
        # rather than snapping back to full first.
        self._utterance = None
        started = self._begin(
            flourish.curve,
            seconds=flourish.seconds,
            reason=reason,
            cue=True,
        )
        if not started:
            # Nothing is running, so nothing is holding the room. Setting the
            # flag before knowing that would leave a cue that never started
            # blocking every sentence for the rest of the session.
            return False

        self._cueing = True
        self.cues += 1
        return True

    # --- driving the lights ------------------------------------------------

    def _begin(self, curve: Curve, seconds: float, reason: str, cue: bool) -> bool:
        """Start replaying a curve, replacing whatever was running."""

        if not self.is_available or seconds <= 0:
            return False

        previous = self._reaction
        if previous is not None and not previous.done():
            # Not awaited: this runs on the bus worker. The cancelled task sees
            # that a newer generation exists and hands the baseline over instead
            # of restoring it.
            previous.cancel()

        self._generation += 1
        self.reactions += 1
        self._reaction = asyncio.create_task(
            self._replay(curve, seconds, reason, cue, self._generation),
            name=f"lighting-{reason}",
        )
        return True

    async def _replay(
        self, curve: Curve, seconds: float, reason: str, cue: bool, generation: int
    ) -> None:
        """Walk a curve in real time. Must not raise, and must not linger."""

        try:
            if not await self._hold():
                return
            await self._follow(curve, seconds, generation)
        except asyncio.CancelledError:
            # Expected: this is how a sentence that was cut short, or a cue
            # taking over, stops the previous reaction.
            raise
        except Exception as error:
            self._record_failure(error)
            logger.exception("lighting_reaction_failed", extra={"lighting_cue": reason})
        finally:
            # Everything on the way out is conditional on still being the
            # current reaction. A task that has been superseded owns nothing:
            # clearing the cue flag or restoring the room from here would be
            # undoing the work of whatever replaced it.
            if self._generation == generation:
                if cue:
                    self._cueing = False
                # The room is this task's to give back — but handed to a
                # separate task rather than restored here, because this one is
                # the one that gets cancelled.
                self._reaction = None
                self._restore_soon()

    async def _follow(self, curve: Curve, seconds: float, generation: int) -> None:
        """Send the curve to the lights until it runs out."""

        started = self._monotonic()
        sent: dict[str, float] = {}
        while True:
            elapsed = self._monotonic() - started
            if elapsed >= seconds:
                return

            targets = self._targets(curve(elapsed))
            if self._worth_sending(targets, sent):
                # A transition as long as the gap to the next command, so the
                # bridge draws the line between them and five updates a second
                # look continuous.
                if await self._apply(targets, self._interval * self._transition_scale):
                    sent = targets
                else:
                    return
            else:
                self.coalesced += 1

            if self._generation != generation:
                return
            await asyncio.sleep(min(self._interval, max(0.0, seconds - elapsed)))

    def _targets(self, factor: float) -> dict[str, float]:
        """Absolute brightness per light for one point on a curve.

        A fraction of each light's own baseline, so a room with one bright lamp
        and one dim one keeps its character while both move.
        """

        return {
            light.light_id: min(
                1.0, max(MIN_BRIGHTNESS, (light.brightness or 0.0) * factor)
            )
            for light in self._dimmable
        }

    def _worth_sending(
        self, targets: Mapping[str, float], sent: Mapping[str, float]
    ) -> bool:
        """Whether any light wants to move far enough to spend a command on.

        This is the coalescing. During the pauses in a sentence the curve barely
        changes, and a command that moves a lamp by less than anybody can see is
        a command taken from the budget for the part of the sentence where it
        would have been visible.
        """

        if not sent:
            return True
        return any(
            abs(value - sent.get(light_id, -1.0)) >= DEADBAND
            for light_id, value in targets.items()
        )

    async def _apply(
        self, targets: Mapping[str, float], transition_seconds: float
    ) -> bool:
        """One command to the lights. Returns whether it worked."""

        try:
            async with asyncio.timeout(self._command_timeout):
                await self._lights.apply(targets, transition_seconds)
        except (LightingCommandError, TimeoutError, OSError) as error:
            self._record_failure(error)
            return False
        self.commands += 1
        self._consecutive_failures = 0
        return True

    # --- holding and giving back the room ----------------------------------

    async def _hold(self) -> bool:
        """Read the baseline, unless it is already held. Returns whether to go on.

        Read exactly once per run of reactions. Reading it again while the lights
        are already dimmed would capture CaddieJ's own modulation as the new
        normal, and every sentence would leave the room a little darker than the
        last — a drift nobody would attribute to the lights.
        """

        if self._baseline is not None:
            return bool(self._dimmable)

        try:
            async with asyncio.timeout(self._command_timeout):
                baseline = await self._lights.snapshot()
        except (LightingCommandError, TimeoutError, OSError) as error:
            self._record_failure(error)
            return False

        # Lights that are off, and switches with no brightness at all, are left
        # alone. Turning somebody's lamp on because a coach started talking is
        # not a subtle reaction to anything.
        dimmable = tuple(
            light
            for light in baseline
            if light.on and light.brightness is not None and light.brightness > 0.0
        )
        if not dimmable:
            # Deliberately not held. A baseline is a debt — something owed back —
            # and there is nothing owed for a room that was never touched, so
            # restoring here would be a command sent to change nothing.
            #
            # It does mean re-reading the room for every line while it stays
            # like this, which is one request a sentence and worth it: somebody
            # who switches a lamp on mid-round gets a reaction to the next one.
            logger.info(
                "lighting_nothing_to_dim", extra={"lighting_lights": len(baseline)}
            )
            return False

        self._baseline = baseline
        self._dimmable = dimmable
        self._interval = self._resolve_interval(len(dimmable))
        return True

    def _resolve_interval(self, lights: int) -> float:
        """How often the lights may be written, given how many there are.

        Derived rather than configured, because the thing with a limit is the
        bridge and the thing that varies is the room. One command per light per
        tick means the budget divides by the number of lights, and a room with
        six lamps in it gets a slower, gentler reaction rather than a bridge
        dropping every third command.
        """

        return max(MIN_INTERVAL_SECONDS, lights / self._budget)

    def _restore_soon(self) -> None:
        """Start giving the room back, without making the bus wait for it.

        Restoring is an HTTP call to an appliance on a home network, and the
        handler that asks for it is running on the bus worker with shots queued
        behind it. So it becomes a task — but its own task, never part of a
        reaction, because reactions are cancelled routinely and a restore
        abandoned half way is the one failure this subsystem must not have.
        """

        if self._baseline is None:
            return
        if self._restoring is not None and not self._restoring.done():
            return
        self._restoring = asyncio.create_task(
            self._give_back(), name="lighting-restore"
        )

    async def _settle(self) -> None:
        """Wait for a restore in flight, so shutdown cannot outrun the room."""

        restoring = self._restoring
        self._restoring = None
        if restoring is not None and not restoring.done():
            await asyncio.gather(restoring, return_exceptions=True)

    async def _give_back(self) -> None:
        """Put the lights back exactly as they were found, and let go.

        Under a lock and re-checked inside it, so that two callers arriving at
        once — a curve running out at the same moment a `SpeechCompleted` lands —
        restore once rather than twice.

        Tried twice, because the two failures worth distinguishing look identical
        from here. A bridge that has been unplugged will fail both times and cost
        one extra timeout, which nobody is waiting on. A bridge that dropped a
        single packet — much the more common of the two on a Zigbee mesh with a
        microwave in the room — succeeds on the second attempt, and the
        difference to the person in the chair is whether their lights come back
        or stay dim for the rest of the evening. Only the last failure is
        counted, so one restore cannot trip the cooldown twice.

        The baseline is cleared only after the attempts, and cleared even when
        they failed. Keeping it would mean re-applying, minutes later, a
        brightness from a room that has since been changed by the person sitting
        in it, which is worse than leaving one sentence's dimming behind.
        """

        async with self._restore_lock:
            baseline = self._baseline
            if not baseline:
                self._baseline = None
                self._dimmable = ()
                return

            try:
                for attempt in range(1, _RESTORE_ATTEMPTS + 1):
                    try:
                        async with asyncio.timeout(self._command_timeout):
                            await self._lights.restore(baseline)
                    except Exception as error:
                        if attempt < _RESTORE_ATTEMPTS:
                            logger.info(
                                "lighting_restore_retrying",
                                extra={"lighting_lights": len(baseline)},
                            )
                            continue
                        self.restore_failures += 1
                        self._record_failure(error)
                        logger.warning("lighting_restore_failed", exc_info=error)
                    else:
                        self.restores += 1
                        self._consecutive_failures = 0
                    break
            finally:
                self._baseline = None
                self._dimmable = ()

    async def _stop(self) -> None:
        """Cancel the reaction, if any, and wait for it to unwind.

        Only the reaction. A restore in flight is left strictly alone; `_settle`
        is how anything waits for that.
        """

        reaction = self._reaction
        self._reaction = None
        self._cueing = False
        if reaction is None or reaction.done():
            return
        reaction.cancel()
        await asyncio.gather(reaction, return_exceptions=True)

    # --- failure -----------------------------------------------------------

    def _record_failure(self, error: object) -> None:
        """Count a failure and, past a point, leave the bridge alone for a while.

        The cooldown is the useful part. A bridge that has been unplugged will
        fail every command, and a reaction that keeps trying turns one absent
        appliance into several timeouts per sentence for the rest of the evening.
        Backing off for a minute and trying again costs nothing and recovers by
        itself when somebody plugs it back in.
        """

        self.failures += 1
        self.last_error = f"{type(error).__name__}: {error}"[:300]
        self._consecutive_failures += 1
        if self._consecutive_failures < self._failure_limit:
            return

        self._consecutive_failures = 0
        self._quiet_until = self._monotonic() + self._cooldown
        logger.warning(
            "lighting_suspended",
            extra={"lighting_cooldown_seconds": round(self._cooldown, 1)},
        )

    # --- reporting ---------------------------------------------------------

    def health(self) -> LightingHealth:
        return LightingHealth(
            enabled=self._enabled,
            available=self.is_available,
            output=self._lights.name,
            lights=len(self._dimmable),
            reacting=self.is_reacting,
            updates_per_second=round(1.0 / self._interval, 2),
            reactions=self.reactions,
            cues=self.cues,
            commands=self.commands,
            coalesced=self.coalesced,
            restores=self.restores,
            failures=self.failures,
            restore_failures=self.restore_failures,
            suspended=self.is_suspended,
            last_error=self.last_error,
        )


def build_lighting_service(
    enabled: bool = False,
    output: str = "hue",
    bridge_host: str = "",
    application_key: str = "",
    lights: str = "",
    minimum: float = 0.82,
    maximum: float = 1.0,
    max_commands_per_second: float = 10.0,
    transition_scale: float = 1.0,
    failure_limit: int = 3,
    cooldown_seconds: float = 60.0,
    command_timeout_seconds: float = 3.0,
) -> LightingService:
    """Assemble lighting from plain values, so tests need no `Settings`."""

    from caddiej.lighting.factory import build_lights

    # Nothing is constructed while lighting is off, so a machine with no HTTP
    # client installed never even resolves the adapter's import.
    port = (
        build_lights(
            output,
            bridge_host=bridge_host,
            application_key=application_key,
            lights=lights,
            timeout_seconds=command_timeout_seconds,
        )
        if enabled
        else None
    )
    return LightingService(
        lights=port,
        modulation=Modulation(minimum=minimum, maximum=maximum),
        enabled=enabled,
        max_commands_per_second=max_commands_per_second,
        transition_scale=transition_scale,
        failure_limit=failure_limit,
        cooldown_seconds=cooldown_seconds,
        command_timeout_seconds=command_timeout_seconds,
    )
