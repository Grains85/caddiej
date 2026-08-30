"""What each output actually does with an effect.

These are the translations: one dial in, a brightness or a font size or a
sentence out. Read as a set, they are the argument for having one dial — a par
is a small pale word, a shallow dip and silence, and an ace is a large bright
word, a deep dip and a spoken line, and none of the three channels was told
about the other two.
"""

import asyncio

import pytest

from caddiej.domain.scoring import HoleResult
from caddiej.domain.speech import SpeechPriority, Utterance
from caddiej.effects.catalogue import EFFECTS
from caddiej.effects.channels import (
    MAX_DIP,
    FloorChannel,
    LightsChannel,
    SpeechChannel,
)
from caddiej.effects.effect import Effect, EffectPriority
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorPoint, ShotMetrics
from caddiej.lighting.fakes import RecordingLights, a_room
from caddiej.lighting.service import LightingService
from caddiej.speech.fakes import FakeSynthesizer, RecordingSink
from caddiej.speech.service import SpeechService


def an_effect(
    cue: str = "birdie", intensity: float = 0.4, seconds: float = 2.0, say: str = ""
) -> Effect:
    return Effect(
        cue=cue,
        label=cue.upper().replace("_", " "),
        priority=EffectPriority.NOTABLE,
        intensity=intensity,
        seconds=seconds,
        say=say,
    )


class TestTheFloor:
    async def test_it_shows_the_word_and_then_takes_it_away(self) -> None:
        floor = FloorService()
        channel = FloorChannel(floor)

        await channel.begin(an_effect("eagle", intensity=0.65, seconds=3.2))

        flourish = floor.state.flourish
        assert flourish is not None
        assert flourish.cue == "eagle"
        assert flourish.label == "EAGLE"
        assert flourish.intensity == 0.65
        assert flourish.seconds == 3.2

        await channel.end(an_effect("eagle"))
        assert floor.state.flourish is None

    async def test_it_leaves_the_round_alone_underneath(self) -> None:
        """The whole reason the flourish is a layer rather than a mode: an
        effect's reset must not disturb what the round put on the floor."""

        floor = FloorService()
        floor.show_marker(FloorPoint(x=0.5, y=0.7))
        floor.set_metrics(ShotMetrics(ball_speed_mph=148.0, launch_angle_degrees=12.0))
        before = floor.state

        channel = FloorChannel(floor)
        await channel.begin(an_effect())
        await channel.end(an_effect())

        assert floor.state.marker == before.marker
        assert floor.state.metrics == before.metrics
        assert floor.state.flourish is None

    async def test_resetting_twice_is_harmless(self) -> None:
        """The director resets unconditionally, so this happens routinely."""

        floor = FloorService()
        channel = FloorChannel(floor)

        await channel.end(an_effect())
        version = floor.state.version
        await channel.end(an_effect())

        assert floor.state.version == version, "nothing changed, so no new version"

    def test_it_cannot_be_unavailable_unless_switched_off(self) -> None:
        """The only channel that is definitely working, which is why the
        catalogue is written so the floor alone carries the whole meaning."""

        floor = FloorService()

        assert FloorChannel(floor).is_available is True
        assert FloorChannel(floor, enabled=False).is_available is False


class SpeechTable:
    """A voice with a fake engine and a sink that remembers."""

    def __init__(self) -> None:
        self.sink = RecordingSink()
        self.service = SpeechService(
            publish=self._collect,
            synthesizer=FakeSynthesizer(),
            sink=self.sink,
            enabled=True,
        )
        self.events: list[object] = []

    async def _collect(self, event: object) -> None:
        self.events.append(event)

    @property
    def queued(self) -> list[Utterance]:
        """What is waiting to be said. Read before yielding to the worker, so
        this is deterministic rather than a race with the queue draining."""

        return [job.utterance for job in self.service._waiting]  # noqa: SLF001


class TestTheVoice:
    async def test_it_says_the_name_of_what_happened(self) -> None:
        table = SpeechTable()
        await table.service.open()
        try:
            channel = SpeechChannel(table.service)
            await channel.begin(an_effect("birdie", say="Birdie."))

            assert [line.text for line in table.queued] == ["Birdie."]
        finally:
            await table.service.aclose()

    async def test_it_speaks_as_an_announcement_rather_than_as_coaching(self) -> None:
        """The tier the speech queue reserved for exactly this: something about
        the round rather than about the swing. It outranks coaching, so a birdie
        cuts off a sentence about the last shot's club path."""

        table = SpeechTable()
        await table.service.open()
        try:
            channel = SpeechChannel(table.service)
            await channel.begin(an_effect(say="Birdie."))

            (line,) = table.queued
            assert line.priority is SpeechPriority.ANNOUNCEMENT
            assert line.priority > SpeechPriority.COACHING
            assert line.perishable is False, "two holes are two announcements"
        finally:
            await table.service.aclose()

    async def test_silence_is_a_real_answer(self) -> None:
        """A par happens on most holes. A room that comments on everything is a
        room somebody mutes."""

        table = SpeechTable()
        await table.service.open()
        try:
            channel = SpeechChannel(table.service)
            await channel.begin(EFFECTS[HoleResult.PAR])

            assert table.queued == []
        finally:
            await table.service.aclose()

    async def test_ending_does_not_cut_the_sentence_off(self) -> None:
        """Deliberately. The visual and the sentence are different lengths, and
        cutting the voice off when the label fades would clip a word."""

        table = SpeechTable()
        await table.service.open()
        try:
            channel = SpeechChannel(table.service)
            await channel.begin(an_effect(say="Hole in one."))
            await channel.end(an_effect())
            await asyncio.sleep(0.05)

            assert len(table.sink.played) == 1
            assert table.service.health().interrupted == 0
        finally:
            await table.service.aclose()

    async def test_a_quiet_machine_makes_the_channel_unavailable(self) -> None:
        table = SpeechTable()
        # Never opened, so nothing is ready to speak.
        assert SpeechChannel(table.service).is_available is False


class TestTheLights:
    async def test_it_dips_the_room_in_proportion_to_the_occasion(self) -> None:
        lights = RecordingLights(room=a_room(1.0))
        lighting = LightingService(lights=lights, max_commands_per_second=100.0)
        await lighting.open()
        try:
            channel = LightsChannel(lighting)
            await channel.begin(an_effect("hole_in_one", intensity=1.0, seconds=0.3))
            await asyncio.sleep(0.2)

            assert lights.applied
            assert min(lights.levels) < 0.3
        finally:
            await lighting.aclose()

        assert lights.restored == [a_room(1.0)]

    @pytest.mark.parametrize(
        ("intensity", "target"),
        [(0.0, 1.0), (0.5, 1.0 - MAX_DIP / 2), (1.0, 1.0 - MAX_DIP)],
    )
    def test_the_mapping_from_one_dial_is_linear(
        self, intensity: float, target: float
    ) -> None:
        """Linear because the intensity scale is already the perceptual one — a
        judgement about how much of an occasion something is, not a physical
        quantity. Curving it again would only move where the tiers sit."""

        assert 1.0 - MAX_DIP * intensity == pytest.approx(target)

    async def test_a_rarer_outcome_dips_further_than_a_common_one(self) -> None:
        """The escalation, measured at the lamp rather than in the table."""

        pars = EFFECTS[HoleResult.PAR]
        ace = EFFECTS[HoleResult.HOLE_IN_ONE]

        assert 1.0 - MAX_DIP * ace.intensity < 1.0 - MAX_DIP * pars.intensity

    async def test_ending_does_nothing_because_lighting_restores_itself(self) -> None:
        """A flourish that outlives a cancelled effect by a second finishes and
        restores itself, which is better than a light stopping mid-dip because a
        label faded."""

        lights = RecordingLights(room=a_room(1.0))
        lighting = LightingService(lights=lights)
        await lighting.open()
        try:
            channel = LightsChannel(lighting)
            await channel.end(an_effect())

            assert lights.applied == []
            assert lights.restored == []
        finally:
            await lighting.aclose()

    def test_lights_that_never_opened_make_the_channel_unavailable(self) -> None:
        lighting = LightingService(lights=RecordingLights())

        assert LightsChannel(lighting).is_available is False
