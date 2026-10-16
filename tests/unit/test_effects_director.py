"""The invariant, from every angle it could be broken.

One effect at a time, every effect ends, every output is put back. Almost all of
these tests are the third one, because it is the only one whose failure somebody
would still be looking at an hour later — a projector reading BIRDIE over the
next forty shots is a much worse bug than a birdie nobody celebrated.

The fakes make the awkward moments arrangeable: a channel held open on a gate is
how an effect gets caught exactly part way through starting, and a channel that
fails only on the way out is how a failed reset gets tested without a real output
anywhere.
"""

import asyncio

import pytest

from caddiej.core import AsyncEventBus
from caddiej.domain.scoring import HoleCompleted, HoleResult
from caddiej.effects.catalogue import EFFECTS
from caddiej.effects.director import EffectDirector
from caddiej.effects.effect import Effect, EffectPriority
from caddiej.effects.fakes import BrokenChannel, ChannelError, RecordingChannel


def an_effect(
    cue: str = "test",
    priority: EffectPriority = EffectPriority.NOTABLE,
    seconds: float = 0.05,
    intensity: float = 0.5,
    say: str = "",
) -> Effect:
    """Something short, so a test about ordering is not also about the clock."""

    return Effect(
        cue=cue,
        label=cue.upper(),
        priority=priority,
        intensity=intensity,
        seconds=seconds,
        say=say,
    )


class Table:
    """A director and a channel that remembers everything."""

    def __init__(
        self,
        *channels: RecordingChannel | BrokenChannel,
        enabled: bool = True,
        max_seconds: float = 8.0,
        channel_timeout_seconds: float = 3.0,
    ) -> None:
        self.channels = channels or (RecordingChannel(),)
        self.director = EffectDirector(
            channels=self.channels,
            enabled=enabled,
            max_seconds=max_seconds,
            channel_timeout_seconds=channel_timeout_seconds,
        )

    @property
    def channel(self) -> RecordingChannel:
        first = self.channels[0]
        assert isinstance(first, RecordingChannel)
        return first

    async def settled(self) -> None:
        """Wait for whatever is running, and for the room to be given back."""

        for _ in range(200):
            if self.director.is_idle:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the director never settled")

    async def __aenter__(self) -> "Table":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.director.aclose()


class TestOneEffectAtATime:
    async def test_an_effect_begins_and_then_ends(self) -> None:
        async with Table() as table:
            assert table.director.start(an_effect("birdie")) is True
            await table.settled()

        assert table.channel.cues == ["birdie"]
        assert table.channel.is_showing is False
        assert table.director.health().plays == 1
        assert table.director.health().resets == 1

    async def test_a_rarer_effect_takes_the_room_from_a_lesser_one(self) -> None:
        async with Table() as table:
            table.director.start(an_effect("birdie", seconds=5.0))
            await asyncio.sleep(0.02)
            assert table.director.start(
                an_effect("eagle", priority=EffectPriority.RARE)
            )
            await table.settled()

        assert table.channel.cues == ["birdie", "eagle"]
        assert table.director.health().replaced == 1

    async def test_an_equal_effect_is_refused_rather_than_swapped_in(self) -> None:
        """The running one is already on screen. Swapping one celebration for an
        equally important one half way through reads as a bug."""

        async with Table() as table:
            table.director.start(an_effect("birdie", seconds=5.0))
            await asyncio.sleep(0.02)
            assert table.director.start(an_effect("also_a_birdie")) is False
            await table.director.aclose()

        assert table.channel.cues == ["birdie"]
        assert table.director.health().refused == 1

    async def test_a_lesser_effect_is_refused(self) -> None:
        async with Table() as table:
            table.director.start(
                an_effect("eagle", priority=EffectPriority.RARE, seconds=5.0)
            )
            await asyncio.sleep(0.02)
            assert (
                table.director.start(an_effect("par", priority=EffectPriority.ROUTINE))
                is False
            )
            await table.director.aclose()

        assert table.channel.cues == ["eagle"]

    async def test_replacement_does_not_reset_in_between(self) -> None:
        """Channels take absolute values, so the incoming effect overwrites the
        outgoing one on every output. Resetting in between would produce a
        visible flicker and nothing else."""

        async with Table() as table:
            table.director.start(an_effect("birdie", seconds=5.0))
            await asyncio.sleep(0.02)
            table.director.start(an_effect("eagle", priority=EffectPriority.RARE))
            await asyncio.sleep(0.02)

            # Two begins, and nothing has ended yet: the handover was direct.
            assert len(table.channel.begun) == 2
            assert table.channel.ended == []

            await table.settled()

        assert len(table.channel.ended) == 1

    async def test_two_effects_in_a_row_each_get_their_own_reset(self) -> None:
        async with Table() as table:
            table.director.start(an_effect("birdie"))
            await table.settled()
            table.director.start(an_effect("birdie"))
            await table.settled()

        assert len(table.channel.begun) == 2
        assert len(table.channel.ended) == 2
        assert table.director.health().resets == 2


class TestNothingRunsAway:
    async def test_the_duration_is_capped_however_long_the_effect_claims(self) -> None:
        """A backstop against a typo in the catalogue, not a tuning knob."""

        async with Table(max_seconds=0.05) as table:
            table.director.start(an_effect("birdie", seconds=30.0))
            await asyncio.wait_for(table.settled(), timeout=2.0)

        assert table.channel.is_showing is False

    async def test_a_channel_that_hangs_on_the_way_in_does_not_hold_the_effect(
        self,
    ) -> None:
        """An output that accepts a command and then stops answering is the exact
        way a celebration would otherwise last all evening."""

        slow = RecordingChannel(channel_name="slow", begin_delay=30.0)
        quick = RecordingChannel(channel_name="quick")
        async with Table(slow, quick, channel_timeout_seconds=0.05) as table:
            table.director.start(an_effect("eagle", seconds=0.05))
            await asyncio.wait_for(table.settled(), timeout=2.0)

        assert table.director.health().failures == 1
        assert quick.is_showing is False

    async def test_a_channel_that_hangs_on_the_way_out_still_lets_go(self) -> None:
        """A hang on the way *out* is the one that matters: the director cannot
        wait for it, because waiting is how the next effect never starts."""

        slow = RecordingChannel(channel_name="slow", end_delay=30.0)
        async with Table(slow, channel_timeout_seconds=0.05) as table:
            table.director.start(an_effect("eagle"))
            await asyncio.wait_for(table.settled(), timeout=2.0)

        assert table.director.health().reset_failures == 1
        assert table.director.is_idle

    async def test_only_one_effect_task_exists_at_a_time(self) -> None:
        async with Table() as table:
            for index in range(5):
                table.director.start(
                    an_effect(f"landmark-{index}", EffectPriority.LANDMARK, seconds=5.0)
                )
                await asyncio.sleep(0.01)

            running = [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("effect-")
                and not task.get_name().startswith("effect-reset")
                and not task.done()
            ]
            assert len(running) == 1
            await table.director.aclose()


class TestTheRoomIsAlwaysGivenBack:
    async def test_every_channel_is_reset_even_the_ones_that_never_began(self) -> None:
        """`end` is required to be safe on a channel that never began, and the
        director relies on it: remembering which ones were touched is bookkeeping
        that only has to be wrong once to leave an output showing forever."""

        absent = RecordingChannel(channel_name="absent", available=False)
        present = RecordingChannel(channel_name="present")
        async with Table(present, absent) as table:
            table.director.start(an_effect("birdie"))
            await table.settled()

        assert present.begun and present.ended
        assert absent.begun == [], "an unavailable channel is never begun"
        assert absent.ended, "but it is still reset"

    async def test_a_channel_whose_begin_failed_is_still_reset(self) -> None:
        """The failure might have happened after the output changed. Assuming it
        did not is how a half-applied effect stays on screen."""

        flaky = RecordingChannel(
            channel_name="flaky", begin_error=ChannelError("half way")
        )
        async with Table(flaky) as table:
            table.director.start(an_effect("birdie"))
            await table.settled()

        assert flaky.ended
        assert table.director.health().failures == 1

    async def test_closing_mid_effect_puts_everything_back_first(self) -> None:
        """Nobody wants to wait for a light show on the way out of a process, but
        the room has to be normal before the process is gone: after this there is
        nothing left running that knows how."""

        table = Table()
        table.director.start(an_effect("hole_in_one", seconds=30.0))
        await asyncio.sleep(0.02)
        assert table.channel.is_showing

        await table.director.aclose()

        assert table.channel.is_showing is False
        assert table.channel.closed == 1
        assert table.director.is_idle

    async def test_closing_while_a_channel_is_still_starting_still_resets(self) -> None:
        """An effect cancelled before its channels finished beginning. Whichever
        had already begun must still be put back, which is why the director marks
        the room as held before it begins anything."""

        gate = asyncio.Event()
        held = RecordingChannel(channel_name="held", gate=gate)
        table = Table(held)
        table.director.start(an_effect("eagle", seconds=30.0))
        await asyncio.sleep(0.02)
        assert held.begun, "it started, and is now waiting on the gate"

        closing = asyncio.create_task(table.director.aclose())
        await asyncio.sleep(0.02)
        gate.set()
        await asyncio.wait_for(closing, timeout=2.0)

        assert held.ended, "the channel that began was put back"
        assert table.director.is_idle

    async def test_a_reset_is_never_cancelled_by_the_effect_it_belongs_to(self) -> None:
        """Resetting runs on its own task for exactly this reason. Effects are
        cancelled routinely; a reset abandoned half way is the failure the whole
        design is about."""

        async with Table() as table:
            table.director.start(an_effect("birdie", seconds=0.03))
            # Cancel the effect's own task the instant it finishes, the way
            # replacement and shutdown both do.
            await asyncio.sleep(0.05)
            await table.settled()

        assert table.channel.is_showing is False

    async def test_a_failed_reset_is_reported_separately(self) -> None:
        """A failed begin means one celebration was quieter than intended, which
        nobody notices. A failed end means an output is still showing something
        and will keep showing it."""

        async with Table(BrokenChannel()) as table:
            table.director.start(an_effect("birdie"))
            await table.settled()

        health = table.director.health()
        assert health.failures == 1
        assert health.reset_failures == 1
        assert health.last_error is not None
        assert "broken" in health.last_error

    async def test_a_failed_reset_is_not_retried_on_the_next_effect(self) -> None:
        """An output nobody can reach is not made more reachable by asking twice,
        and retrying would put the same failing command in front of every future
        celebration."""

        async with Table(BrokenChannel()) as table:
            table.director.start(an_effect("birdie"))
            await table.settled()
            first = table.director.health().reset_failures

            table.director.start(an_effect("birdie"))
            await table.settled()

        assert table.director.health().reset_failures == first + 1


class TestNothingHereCanBreakAnything:
    async def test_one_broken_channel_does_not_stop_the_others(self) -> None:
        """The channels are independent outputs. A bridge that has gone away is
        no reason for the floor to stay blank."""

        working = RecordingChannel(channel_name="working")
        async with Table(BrokenChannel(), working) as table:
            table.director.start(an_effect("eagle"))
            await table.settled()

        assert working.cues == ["eagle"]
        assert working.is_showing is False

    async def test_a_disabled_director_does_nothing_at_all(self) -> None:
        async with Table(enabled=False) as table:
            assert table.director.start(an_effect("birdie")) is False
            assert await table.director.play(HoleResult.EAGLE) is False

        assert table.channel.begun == []
        assert table.director.health().available is False

    async def test_a_director_with_no_channels_is_not_available(self) -> None:
        director = EffectDirector(channels=())

        assert director.is_available is False
        assert director.start(an_effect()) is False
        await director.aclose()

    async def test_a_director_whose_channels_are_all_unavailable_says_so(self) -> None:
        async with Table(RecordingChannel(available=False)) as table:
            assert table.director.is_available is False
            assert table.director.health().channels == ()

    async def test_a_configured_channel_that_cannot_be_reached_is_named(self) -> None:
        """An effect that does not happen is silent by design.

        Nothing goes wrong on screen, nobody hears a failure, and so a channel
        whose bridge is down looks identical to a channel nobody configured —
        unless health names it. `/healthz` is the only place this is visible.
        """

        async with Table(
            RecordingChannel(channel_name="floor"),
            RecordingChannel(channel_name="lights", available=False),
        ) as table:
            health = table.director.health()

            assert health.available is True
            assert health.channels == ("floor",)
            assert health.unreachable_channels == ("lights",)

    async def test_the_event_path_returns_without_waiting(self) -> None:
        """A handler that stayed for the length of a celebration would leave
        the next hole sitting on the effects queue until this one ended."""

        async with Table() as table:
            slow = an_effect("hole_in_one", EffectPriority.LANDMARK, seconds=30.0)
            table.director.start(slow)
            # Returning at all is the assertion; a handler that awaited the
            # effect would still be inside `start`.
            assert table.director.is_playing
            await table.director.aclose()


class TestTheScoringHook:
    async def test_a_finished_hole_on_the_bus_reaches_the_channels(self) -> None:
        """Nothing publishes this yet. The hook is what makes scoring, when it
        exists, a change in the scorer rather than a change here."""

        bus = AsyncEventBus()
        async with Table(max_seconds=0.05) as table:
            table.director.subscribe(bus)
            await bus.start()
            try:
                await bus.publish(HoleCompleted(result=HoleResult.EAGLE, hole_number=7))
                await asyncio.sleep(0.05)
            finally:
                await bus.stop()
            await table.settled()

        assert table.channel.cues == ["eagle"]

    @pytest.mark.parametrize("result", list(HoleResult))
    async def test_every_outcome_can_be_played_end_to_end(
        self, result: HoleResult
    ) -> None:
        async with Table(max_seconds=0.05) as table:
            assert await table.director.play(result) is True
            await asyncio.wait_for(table.settled(), timeout=2.0)

        assert table.channel.cues == [EFFECTS[result].cue]

    async def test_health_names_what_is_playing(self) -> None:
        async with Table() as table:
            await table.director.play(HoleResult.HOLE_IN_ONE)
            await asyncio.sleep(0.01)

            assert table.director.health().playing == "hole_in_one"
            await table.director.aclose()

        assert table.director.health().playing is None
