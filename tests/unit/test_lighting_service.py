"""What the room actually does, and everything that must not happen to it.

The promise under test throughout is the one a person in an armchair would
notice: the lights follow the voice, they go back to exactly what they were, and
nothing about a bridge on a home network can reach the golf.
"""

import asyncio
from uuid import UUID, uuid4

import pytest

from caddiej.domain.events import SpeechCompleted, SpeechStarted
from caddiej.domain.speech import Envelope, SpeechOutcome, SpeechPriority
from caddiej.lighting.fakes import BrokenLights, RecordingLights, a_room
from caddiej.lighting.modulation import Flourish, Modulation
from caddiej.lighting.port import LightingCommandError, LightingUnavailableError
from caddiej.lighting.service import MIN_BRIGHTNESS, LightingService

# Gestures at three depths. Named after the outcomes that produce them in
# `caddiej.effects`, because that is what makes the assertions readable — but
# this subsystem is handed a depth and a duration and knows nothing else.
SMALL = Flourish(target=0.74, seconds=0.9)
MIDDLING = Flourish(target=0.52, seconds=1.3)
DEEP = Flourish(target=0.16, seconds=2.4)


def an_envelope(*values: float, frame_rate: float = 20.0) -> Envelope:
    return Envelope(values=values, frame_rate=frame_rate)


def loud(seconds: float = 1.0, frame_rate: float = 20.0) -> Envelope:
    """A line that is at full level throughout, so the curve is predictable."""

    return Envelope(
        values=tuple(1.0 for _ in range(round(seconds * frame_rate))),
        frame_rate=frame_rate,
    )


class Table:
    """A service, a room, and the two events that drive it."""

    def __init__(
        self,
        lights: RecordingLights | BrokenLights | None = None,
        modulation: Modulation | None = None,
        **options: object,
    ) -> None:
        self.lights = RecordingLights() if lights is None else lights
        self.service = LightingService(
            lights=self.lights,  # type: ignore[arg-type]
            modulation=modulation,
            enabled=True,
            **options,  # type: ignore[arg-type]
        )

    async def __aenter__(self) -> "Table":
        await self.service.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.service.aclose()

    async def speaks(
        self,
        envelope: Envelope | None = None,
        seconds: float = 1.0,
        utterance_id: UUID | None = None,
    ) -> UUID:
        identifier = utterance_id or uuid4()
        await self.service.on_speech_started(
            SpeechStarted(
                utterance_id=identifier,
                text="Square the face.",
                voice="bm_george",
                priority=SpeechPriority.COACHING,
                duration_seconds=seconds,
                envelope=loud(seconds) if envelope is None else envelope,
            )
        )
        return identifier

    async def stops(
        self,
        utterance_id: UUID,
        outcome: SpeechOutcome = SpeechOutcome.SPOKEN,
    ) -> None:
        await self.service.on_speech_completed(
            SpeechCompleted(utterance_id=utterance_id, outcome=outcome)
        )

    async def settled(self, timeout: float = 5.0) -> None:
        """Let the reaction and any restore finish."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.service.is_idle:
                await asyncio.sleep(0)
                if self.service.is_idle:
                    return
            await asyncio.sleep(0.005)
        raise AssertionError("the lighting service never went idle")


class TestFollowingAVoice:
    async def test_the_lights_move_while_a_line_plays(self) -> None:
        async with Table() as table:
            utterance = await table.speaks(seconds=0.4)
            await asyncio.sleep(0.25)
            await table.stops(utterance)
            await table.settled()

        assert table.lights.applied, "the lights were never written to"
        assert table.lights.snapshots == 1

    async def test_brightness_follows_the_measured_shape(self) -> None:
        """The whole point of the feature: a quiet passage is dimmer than a loud
        one, because the samples said so."""

        quiet_then_loud = an_envelope(*([0.0] * 10 + [1.0] * 10), frame_rate=50.0)
        async with Table(
            modulation=Modulation(minimum=0.4, maximum=1.0),
            max_commands_per_second=100.0,
        ) as table:
            utterance = await table.speaks(quiet_then_loud, seconds=0.4)
            await asyncio.sleep(0.45)
            await table.stops(utterance)
            await table.settled()

        levels = table.lights.levels
        assert min(levels) < 0.5, "the quiet half should have dimmed the room"
        assert max(levels) > 0.9, "the loud half should have restored it"

    async def test_each_light_moves_by_a_fraction_of_its_own_brightness(self) -> None:
        """A room with a bright lamp and a dim one keeps its character."""

        async with Table(
            lights=RecordingLights(room=a_room(1.0, 0.4)),
            modulation=Modulation(minimum=0.4, maximum=0.5),
        ) as table:
            # A line at full level throughout, so the factor is the maximum and
            # the only variable left is each lamp's own brightness.
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        first, _ = table.lights.applied[0]
        assert first["light-1"] == pytest.approx(0.5)
        assert first["light-2"] == pytest.approx(0.2)

    async def test_a_line_with_no_measured_shape_is_left_alone(self) -> None:
        """A room pulsing to a shape nobody measured would eventually disagree
        with what somebody can hear."""

        async with Table() as table:
            await table.speaks(an_envelope())
            await table.service.on_speech_started(
                SpeechStarted(
                    utterance_id=uuid4(),
                    text="No envelope at all.",
                    voice="bm_george",
                    priority=SpeechPriority.COACHING,
                    duration_seconds=1.0,
                )
            )
            await table.settled()

        assert table.lights.applied == []
        assert table.lights.snapshots == 0

    async def test_a_flat_modulation_range_switches_the_reaction_off(self) -> None:
        async with Table(modulation=Modulation(minimum=1.0, maximum=1.0)) as table:
            await table.speaks()
            await table.settled()

        assert table.lights.applied == []


class TestGivingTheRoomBack:
    async def test_the_room_is_restored_to_exactly_what_it_was(self) -> None:
        room = a_room(0.63, 0.21)
        async with Table(lights=RecordingLights(room=room)) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        assert table.lights.restored == [room]

    async def test_a_line_cut_short_still_gives_the_room_back(self) -> None:
        """An interrupted sentence leaves the reaction part way through a dip,
        and a room that stayed there until the next shot would look broken."""

        async with Table(lights=RecordingLights(room=a_room(0.8))) as table:
            utterance = await table.speaks(seconds=30.0)
            await asyncio.sleep(0.1)
            await table.stops(utterance, SpeechOutcome.INTERRUPTED)
            await table.settled()

        assert table.lights.restored == [a_room(0.8)]
        assert table.service.health().restores == 1

    async def test_the_room_comes_back_when_the_line_simply_runs_out(self) -> None:
        """No `SpeechCompleted` needed. The curve knows how long the line was."""

        async with Table(lights=RecordingLights(room=a_room(0.5))) as table:
            await table.speaks(seconds=0.15)
            await table.settled()

        assert table.lights.restored == [a_room(0.5)]

    async def test_shutting_down_mid_sentence_gives_the_room_back(self) -> None:
        """The one piece of shutdown that is visible from an armchair."""

        table = Table(lights=RecordingLights(room=a_room(0.9)))
        await table.service.open()
        await table.speaks(seconds=30.0)
        await asyncio.sleep(0.1)
        await table.service.aclose()

        assert table.lights.restored == [a_room(0.9)]
        assert table.lights.closed == 1

    async def test_the_baseline_is_read_once_and_never_re_read_while_dimmed(
        self,
    ) -> None:
        """The worst bug this subsystem could have: measuring CaddieJ's own
        dimming as the new normal, so every sentence leaves the room darker."""

        room = a_room(1.0)
        async with Table(lights=RecordingLights(room=room)) as table:
            for _ in range(3):
                utterance = await table.speaks(seconds=0.2)
                await asyncio.sleep(0.05)
                await table.stops(utterance)
                await table.settled()

        assert table.lights.snapshots == 3
        assert table.lights.restored == [room, room, room]

    async def test_back_to_back_lines_hold_the_baseline_across_the_gap(self) -> None:
        """No snapshot and no restore in between, so the room does not flick back
        to full brightness between two sentences."""

        async with Table() as table:
            first = await table.speaks(seconds=5.0)
            await asyncio.sleep(0.05)
            await table.speaks(seconds=0.2)
            await asyncio.sleep(0.05)
            assert table.lights.snapshots == 1
            assert table.lights.restored == []
            await table.stops(first)
            await table.settled()

        assert table.lights.snapshots == 1

    async def test_a_completion_for_some_other_line_changes_nothing(self) -> None:
        """Most `SpeechCompleted` events describe utterances that never made a
        sound, and treating one as a reason to restore would cancel the reaction
        to the line that is actually playing."""

        async with Table() as table:
            utterance = await table.speaks(seconds=5.0)
            await asyncio.sleep(0.05)
            await table.stops(uuid4(), SpeechOutcome.SUPERSEDED)
            await asyncio.sleep(0.05)

            assert table.lights.restored == []
            assert table.service.is_reacting

            await table.stops(utterance)
            await table.settled()

        assert len(table.lights.restored) == 1


class TestTheRoomItself:
    async def test_a_light_that_is_off_is_not_turned_on(self) -> None:
        """Turning somebody's lamp on because a coach started talking is not a
        subtle reaction to anything."""

        async with Table(lights=RecordingLights(room=a_room(0.8, on=False))) as table:
            await table.speaks(seconds=0.3)
            await table.settled()

        assert table.lights.applied == []
        # Nothing was touched, so nothing is owed back: a restore here would be
        # a command sent to change nothing.
        assert table.lights.restored == []

    async def test_a_switch_with_no_brightness_is_left_alone(self) -> None:
        from caddiej.lighting.port import LightState

        room = (LightState(light_id="switch", on=True, brightness=None),)
        async with Table(lights=RecordingLights(room=room)) as table:
            await table.speaks(seconds=0.3)
            await table.settled()

        assert table.lights.applied == []

    async def test_a_room_with_one_dimmable_lamp_among_several_uses_it(self) -> None:
        from caddiej.lighting.port import LightState

        room = (
            LightState(light_id="off", on=False, brightness=0.9),
            LightState(light_id="switch", on=True, brightness=None),
            LightState(light_id="lamp", on=True, brightness=0.6),
        )
        async with Table(lights=RecordingLights(room=room)) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        assert table.lights.applied
        assert all(set(command) == {"lamp"} for command, _ in table.lights.applied)
        # Everything is handed back, including what was never touched, because
        # the port decides what a restore consists of.
        assert table.lights.restored == [room]

    async def test_brightness_never_reaches_zero(self) -> None:
        """Zero means off to most vendors, and off is not a brightness."""

        async with Table(
            lights=RecordingLights(room=a_room(0.02)),
            modulation=Modulation(minimum=0.01, maximum=0.01),
        ) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        assert all(
            value >= MIN_BRIGHTNESS
            for command, _ in table.lights.applied
            for value in command.values()
        )


class TestRespectingTheBridge:
    async def test_the_tick_is_derived_from_the_budget_and_the_room(self) -> None:
        """A room with more lamps in it gets a slower, gentler reaction rather
        than a bridge dropping every third command."""

        async with Table(
            lights=RecordingLights(room=a_room(1.0, 1.0, 1.0, 1.0)),
            max_commands_per_second=8.0,
        ) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.05)
            # Four lamps against a budget of eight commands a second is two
            # ticks a second, not eight.
            assert table.service.health().updates_per_second == 2.0
            await table.stops(utterance)
            await table.settled()

    async def test_the_command_rate_stays_inside_the_budget(self) -> None:
        async with Table(
            lights=RecordingLights(room=a_room(1.0, 1.0)),
            max_commands_per_second=10.0,
        ) as table:
            # A steadily rising line, so the deadband never suppresses anything
            # and the only thing limiting the traffic is the budget.
            envelope = an_envelope(
                *[index / 25 for index in range(25)], frame_rate=50.0
            )
            utterance = await table.speaks(envelope, seconds=0.5)
            await asyncio.sleep(0.55)
            await table.stops(utterance)
            await table.settled()

        # Two lamps against ten commands a second is five ticks a second, so
        # half a second allows three, plus one for loop overhead.
        assert 1 <= len(table.lights.applied) <= 4

    async def test_each_command_asks_for_a_transition_covering_the_next_gap(
        self,
    ) -> None:
        """Which is what makes five updates a second look continuous rather than
        like a staircase."""

        async with Table(max_commands_per_second=5.0) as table:
            utterance = await table.speaks(seconds=0.6)
            await asyncio.sleep(0.3)
            await table.stops(utterance)
            await table.settled()

        transitions = [transition for _, transition in table.lights.applied]
        assert transitions
        assert transitions == [pytest.approx(0.2)] * len(transitions)

    async def test_a_movement_nobody_could_see_is_not_sent(self) -> None:
        """The coalescing. A command that moves a lamp by less than anybody can
        see is a command taken from the budget for one that would be visible."""

        async with Table(
            modulation=Modulation(minimum=0.9, maximum=1.0),
            max_commands_per_second=100.0,
        ) as table:
            # A completely flat line: after the first command there is nothing
            # left to say.
            utterance = await table.speaks(loud(0.4, frame_rate=50.0), seconds=0.4)
            await asyncio.sleep(0.45)
            await table.stops(utterance)
            await table.settled()

        # One command, then several ticks that each decided there was nothing
        # worth saying.
        health = table.service.health()
        assert health.commands == 1
        assert health.coalesced > health.commands


class TestCelebrations:
    """A gesture runs for a second or two of real time, so these assert that one
    started and then close rather than sitting through it. Closing cancels and
    restores, which is the same path a gesture interrupted by shutdown takes."""

    async def test_a_gesture_moves_the_room_and_gives_it_back(self) -> None:
        async with Table(lights=RecordingLights(room=a_room(0.8))) as table:
            assert await table.service.play(SMALL, reason="birdie") is True
            await asyncio.sleep(0.1)
            assert table.lights.applied
            assert table.service.health().cues == 1

        assert table.lights.restored == [a_room(0.8)]

    async def test_a_gesture_is_all_this_subsystem_is_told(self) -> None:
        """No golf reaches here. A depth and a duration are the whole request,
        and the reason is a label for a log rather than something to interpret."""

        async with Table() as table:
            assert await table.service.play(Flourish(target=0.5, seconds=0.4)) is True
            await asyncio.sleep(0.1)
            assert table.lights.applied

    async def test_a_celebration_dims_the_room_further_than_a_sentence(self) -> None:
        """Deliberately more noticeable than coaching: the room is marking
        something rather than keeping somebody company."""

        async with Table(max_commands_per_second=100.0) as table:
            await table.service.play(DEEP, reason="hole_in_one")
            await asyncio.sleep(0.4)
            cue = min(table.lights.levels)

        async with Table(max_commands_per_second=100.0) as table:
            utterance = await table.speaks(loud(0.3, frame_rate=50.0), seconds=0.3)
            await asyncio.sleep(0.2)
            await table.stops(utterance)
            spoken = min(table.lights.levels)

        assert cue < spoken

    async def test_a_celebration_keeps_the_room_from_a_sentence(self) -> None:
        """It is rarer and more interesting than anything being said over the top
        of it."""

        async with Table() as table:
            await table.service.play(DEEP, reason="hole_in_one")
            await asyncio.sleep(0.1)
            commands = len(table.lights.applied)

            await table.speaks(seconds=1.0)
            await asyncio.sleep(0.1)

            assert table.lights.applied[commands:], "the cue should still be running"
            assert table.service.health().reactions == 1

    async def test_a_cue_that_could_not_start_does_not_block_later_speech(
        self,
    ) -> None:
        """The flag that says "a celebration owns the room" must only be set once
        one actually does. Setting it before knowing would leave a cue that never
        started silencing the lights for the rest of the session."""

        async with Table(lights=BrokenLights(), failure_limit=1) as table:
            # One failure puts the bridge in its cooldown.
            await table.service.play(MIDDLING, reason="eagle")
            await table.settled()
            assert table.service.is_suspended

            # This one cannot even begin.
            assert await table.service.play(SMALL, reason="birdie") is False
            assert table.service.health().cues == 1
            assert table.service._cueing is False  # noqa: SLF001

    async def test_a_cue_that_failed_hands_the_room_back_to_speech(self) -> None:
        """A gesture whose bridge fell over must not leave the flag set either."""

        async with Table(
            lights=RecordingLights(snapshot_error=LightingCommandError("no answer"))
        ) as table:
            await table.service.play(MIDDLING, reason="eagle")
            await table.settled()

            assert table.service._cueing is False  # noqa: SLF001

    async def test_two_cues_in_a_row_leave_the_second_owning_the_room(self) -> None:
        """The superseded gesture must not clear the flag on its way out, or a
        sentence arriving during the second cue would take the room from it."""

        async with Table() as table:
            await table.service.play(SMALL, reason="birdie")
            await asyncio.sleep(0.02)
            await table.service.play(DEEP, reason="hole_in_one")
            await asyncio.sleep(0.1)

            commands = len(table.lights.applied)
            await table.speaks(seconds=1.0)
            await asyncio.sleep(0.1)

            assert table.lights.applied[commands:], "the cue should still be running"
            assert table.service.health().reactions == 2

    async def test_a_celebration_takes_the_room_from_a_sentence(self) -> None:
        async with Table(lights=RecordingLights(room=a_room(1.0))) as table:
            utterance = await table.speaks(seconds=30.0)
            await asyncio.sleep(0.05)
            await table.service.play(MIDDLING, reason="eagle")
            await asyncio.sleep(0.05)

            # Handed over without restoring in between, so the lights move from
            # where they are into the gesture rather than snapping back first.
            assert table.lights.restored == []
            assert table.lights.snapshots == 1
            await table.stops(utterance)

        assert table.lights.restored == [a_room(1.0)]


class TestNothingHereCanBreakAnything:
    async def test_a_bridge_that_will_not_answer_costs_a_counter(self) -> None:
        async with Table(lights=BrokenLights()) as table:
            await table.speaks(seconds=0.3)
            await table.settled()

        health = table.service.health()
        assert health.failures >= 1
        assert health.commands == 0
        assert health.last_error is not None

    async def test_a_bridge_that_fails_repeatedly_is_left_alone_for_a_while(
        self,
    ) -> None:
        """A bridge that has been unplugged fails everything, and retrying it
        once per syllable for an evening is worse than waiting."""

        async with Table(
            lights=BrokenLights(), failure_limit=2, cooldown_seconds=30.0
        ) as table:
            for _ in range(4):
                await table.speaks(seconds=0.2)
                await table.settled()

            assert table.service.is_suspended
            assert table.service.is_available is False
            calls = table.lights.calls

            await table.speaks(seconds=0.2)
            await table.settled()
            assert table.lights.calls == calls, "a suspended bridge was touched"

    async def test_a_suspended_bridge_is_tried_again_once_the_cooldown_passes(
        self,
    ) -> None:
        clock = 1000.0
        table = Table(
            lights=BrokenLights(),
            failure_limit=1,
            cooldown_seconds=60.0,
            monotonic=lambda: clock,
        )
        await table.service.open()
        try:
            await table.speaks(seconds=0.2)
            await table.settled()
            assert table.service.is_suspended

            clock += 61.0
            assert table.service.is_suspended is False
            assert table.service.is_available is True
        finally:
            await table.service.aclose()

    async def test_a_command_that_hangs_is_abandoned_not_waited_on(self) -> None:
        async with Table(
            lights=RecordingLights(apply_delay=5.0), command_timeout_seconds=0.1
        ) as table:
            await table.speaks(seconds=0.3)
            await table.settled()

        assert table.service.health().failures >= 1

    async def test_a_snapshot_that_fails_means_the_room_is_not_touched(self) -> None:
        """Without a baseline there is no way back, and a room left at a
        brightness nobody chose is worse than a room that did not react."""

        async with Table(
            lights=RecordingLights(snapshot_error=LightingCommandError("no answer"))
        ) as table:
            await table.speaks(seconds=0.3)
            await table.settled()

        assert table.lights.applied == []
        assert table.lights.restored == []
        assert table.service.health().failures == 1

    async def test_a_restore_that_fails_is_counted_separately(self) -> None:
        """It is the one failure somebody in the room would notice, so it does
        not hide among the others."""

        async with Table(
            lights=RecordingLights(restore_error=LightingCommandError("gone"))
        ) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        health = table.service.health()
        assert health.restore_failures == 1
        assert health.restores == 0

    async def test_a_restore_is_tried_a_second_time_before_giving_up(self) -> None:
        """The one command worth retrying, because it is the only one whose
        failure somebody is left living with. A dropped packet on a mesh with a
        microwave in the room is indistinguishable from a dead bridge here, and
        the difference to the person in the chair is whether their lights come
        back or stay dim for the rest of the evening."""

        async with Table(lights=RecordingLights(restore_failures=1)) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        assert table.lights.restore_attempts == 2
        assert table.lights.restored == [a_room(1.0)]
        health = table.service.health()
        assert health.restores == 1
        assert health.restore_failures == 0, "it worked, so nothing failed"

    async def test_a_bridge_that_is_gone_is_counted_once_not_twice(self) -> None:
        """Otherwise one restore trips the consecutive-failure cooldown by
        itself, and lighting switches off over a single dark room."""

        async with Table(
            lights=RecordingLights(restore_error=LightingCommandError("gone"))
        ) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            await table.stops(utterance)
            await table.settled()

        assert table.lights.restore_attempts == 2
        assert table.service.health().restore_failures == 1

    async def test_a_bridge_that_cannot_be_opened_leaves_lighting_switched_off(
        self,
    ) -> None:
        table = Table(
            lights=RecordingLights(
                open_error=LightingUnavailableError("no such bridge")
            )
        )
        await table.service.open()
        try:
            assert table.service.is_available is False
            await table.speaks(seconds=0.3)
            assert table.lights.applied == []
        finally:
            await table.service.aclose()

        assert table.service.health().last_error is not None

    async def test_an_unconfigured_output_is_never_opened(self) -> None:
        table = Table(lights=RecordingLights(configured=False))
        await table.service.open()
        try:
            assert table.lights.opened == 0
            assert table.service.is_available is False
        finally:
            await table.service.aclose()

    async def test_lighting_switched_off_touches_nothing(self) -> None:
        lights = RecordingLights()
        service = LightingService(lights=lights, enabled=False)  # type: ignore[arg-type]
        await service.open()
        try:
            assert service.is_available is False
            assert lights.opened == 0
        finally:
            await service.aclose()

        assert lights.applied == []


class TestLifecycle:
    async def test_opening_and_closing_with_nothing_to_do_is_clean(self) -> None:
        async with Table() as table:
            assert table.service.is_available is True
            assert table.service.is_idle is True

        assert table.lights.opened == 1
        assert table.lights.closed == 1
        assert table.lights.restored == []

    async def test_closing_twice_is_harmless(self) -> None:
        table = Table()
        await table.service.open()
        await table.service.aclose()
        await table.service.aclose()

        assert table.lights.closed == 2

    async def test_health_reports_the_room_and_everything_that_stopped_it(
        self,
    ) -> None:
        async with Table(lights=RecordingLights(room=a_room(1.0, 0.5))) as table:
            utterance = await table.speaks(seconds=0.3)
            await asyncio.sleep(0.1)
            health = table.service.health()
            assert health.enabled is True
            assert health.available is True
            assert health.output == "recording"
            assert health.lights == 2
            assert health.reacting is True

            await table.stops(utterance)
            await table.settled()

        after = table.service.health()
        assert after.reactions == 1
        assert after.restores == 1
        assert after.commands >= 1
        assert after.failures == 0

    @pytest.mark.parametrize(
        "options",
        [
            {"max_commands_per_second": 0.0},
            {"max_commands_per_second": -1.0},
            {"failure_limit": 0},
        ],
    )
    def test_an_impossible_configuration_is_refused_at_construction(
        self, options: dict[str, float]
    ) -> None:
        with pytest.raises(ValueError):
            LightingService(**options)  # type: ignore[arg-type]
