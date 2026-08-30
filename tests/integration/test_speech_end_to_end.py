"""A shot going all the way from the launch monitor to a voice in the room.

The units are covered elsewhere. This is about the wiring: a real event bus, the
real coaching service, the real speech service and the real queue, with only the
two things that touch hardware faked — the model that writes the words and the
speaker that plays them.

The path being checked is the one the milestone describes:

    a swing
        -> a deterministic diagnosis
        -> a provider
        -> analysis.completed
        -> a speech queue
        -> a rendered voice
        -> speech.started, then speech.completed

Nothing here spends anything, downloads anything, or makes a sound. The provider
is scripted and the engine is a fake, which is also how the whole path gets
reviewed on a machine with no credentials and no speech model installed.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.coaching.fakes import GOOD_REPLY, BrokenProvider, ScriptedProvider
from caddiej.coaching.provider import ProviderRefusedError
from caddiej.coaching.service import AnalysisService
from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.events import (
    AnalysisCompleted,
    ShotCompleted,
    ShotDiagnosed,
    SpeechCompleted,
    SpeechStarted,
)
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)
from caddiej.domain.speech import SpeechOutcome, SpeechPriority, Utterance
from caddiej.speech.cache import AudioCache
from caddiej.speech.fakes import FakeSynthesizer, RecordingSink
from caddiej.speech.service import SpeechService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


def a_shot(face: float = 6.5) -> Shot:
    """A shot with an open face, so there is always something to coach."""

    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=148.0,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            total_spin_rpm=2400.0,
            carry_yards=265.0,
        ),
        club_delivery=ClubDelivery(
            speed_mph=112.0,
            path_degrees=-1.0,
            face_to_target_degrees=face,
            angle_of_attack_degrees=-1.0,
        ),
        handedness=Handedness.RIGHT,
        club=Club(kind=ClubKind.DRIVER, vendor_code="D"),
    )


class Table:
    """Coaching and speech wired to one real bus, so a test can hit a ball."""

    def __init__(
        self,
        provider: object | None = None,
        engine: FakeSynthesizer | None = None,
        sink: RecordingSink | None = None,
        speech_enabled: bool = True,
    ) -> None:
        self.bus = AsyncEventBus()
        self.seen: list[Event] = []
        self.engine = engine if engine is not None else FakeSynthesizer()
        self.sink = sink if sink is not None else RecordingSink()

        self.coaching = AnalysisService(
            publish=self.bus.publish,
            provider=provider if provider is not None else ScriptedProvider(),  # type: ignore[arg-type]
            # Nobody hits two balls inside eight seconds, but a test does.
            min_interval_seconds=0.0,
        )
        self.speech = SpeechService(
            publish=self.bus.publish,
            synthesizer=self.engine,
            sink=self.sink,
            cache=AudioCache(),
            enabled=speech_enabled,
        )

    async def __aenter__(self) -> "Table":
        for kind in (ShotDiagnosed, AnalysisCompleted, SpeechStarted, SpeechCompleted):
            self.bus.subscribe(kind, self._remember)
        await self.bus.start()
        await self.coaching.open()
        self.coaching.subscribe(self.bus)
        await self.speech.open()
        self.speech.subscribe(self.bus)
        return self

    async def __aexit__(self, *_: object) -> None:
        # The same order the application uses: speech first, so nothing starts
        # talking while everything else is stopping.
        await self.speech.aclose()
        await self.coaching.aclose()
        await self.bus.stop()

    async def _remember(self, event: Event) -> None:
        self.seen.append(event)

    async def hit(self, shot: Shot | None = None) -> None:
        await self.bus.publish(ShotCompleted(shot=shot or a_shot()))

    async def settled(self, timeout: float = 3.0) -> None:
        """Let the bus, coaching and speech all come to rest."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        turns = 0
        while loop.time() < deadline:
            if self.bus.is_idle and self.speech.is_idle:
                await asyncio.sleep(0)
                if self.bus.is_idle and self.speech.is_idle:
                    return
            turns += 1
            await asyncio.sleep(0 if turns < 200 else 0.001)
        raise AssertionError("the pipeline never came to rest")

    def of[EventType: Event](self, kind: type[EventType]) -> list[EventType]:
        return [event for event in self.seen if isinstance(event, kind)]


class TestASwingBecomesAVoice:
    async def test_the_line_a_model_wrote_is_the_line_that_is_spoken(self) -> None:
        async with Table() as table:
            await table.hit()
            await table.settled()

        assert table.of(AnalysisCompleted)
        assert table.engine.calls == [(GOOD_REPLY["speech"], "bm_george")]
        assert len(table.sink.played) == 1

    async def test_both_speech_events_reach_the_bus(self) -> None:
        async with Table() as table:
            await table.hit()
            await table.settled()

        started = table.of(SpeechStarted)
        completed = table.of(SpeechCompleted)
        assert len(started) == 1
        assert len(completed) == 1
        assert completed[0].outcome is SpeechOutcome.SPOKEN
        assert started[0].utterance_id == completed[0].utterance_id

    async def test_the_spoken_line_names_the_shot_it_was_about(self) -> None:
        """So a listener can line up what is being said with what is on the
        floor, without either of them knowing the other exists."""

        shot = a_shot()
        async with Table() as table:
            await table.hit(shot)
            await table.settled()

        assert table.of(SpeechStarted)[0].shot_id == shot.shot_id

    async def test_the_deterministic_verdict_still_arrives_first(self) -> None:
        """The floor must never be waiting on a voice. `ShotDiagnosed` is
        published on the event path and speech happens on a worker, so their
        order is the whole point."""

        async with Table() as table:
            await table.hit()
            await table.settled()

        kinds = [type(event).__name__ for event in table.seen]
        assert kinds.index("ShotDiagnosed") < kinds.index("SpeechStarted")


class TestNothingWorthSaying:
    async def test_a_good_shot_is_not_spoken_about(self) -> None:
        """Speech follows the coached line, and a shot with nothing wrong with
        it never produces one. A machine that talked after every swing would be
        turned off within an evening."""

        async with Table() as table:
            await table.hit(a_shot(face=0.2))
            await table.settled()

        assert table.of(AnalysisCompleted) == []
        assert table.engine.calls == []
        assert table.of(SpeechStarted) == []


class TestFailureIsInvisible:
    async def test_a_provider_that_refuses_leaves_the_room_quiet(self) -> None:
        """The templated line is already on the floor. Nothing raises, and the
        next shot works."""

        provider = BrokenProvider(error=ProviderRefusedError("wrong key"))
        async with Table(provider=provider) as table:
            await table.hit()
            await table.settled()

            assert table.of(SpeechStarted) == []
            assert table.of(ShotDiagnosed)

    async def test_a_voice_that_will_not_load_does_not_affect_the_shot(self) -> None:
        from caddiej.speech.synth import SynthesisUnavailableError

        engine = FakeSynthesizer(
            open_error=SynthesisUnavailableError("no model on this machine")
        )
        async with Table(engine=engine) as table:
            await table.hit()
            await table.settled()

            assert table.speech.is_available is False
            assert table.of(ShotDiagnosed)
            assert table.of(AnalysisCompleted)
            assert table.of(SpeechStarted) == []

    async def test_speech_switched_off_changes_nothing_upstream(self) -> None:
        async with Table(speech_enabled=False) as table:
            await table.hit()
            await table.settled()

            assert table.of(ShotDiagnosed)
            assert table.of(AnalysisCompleted)
            assert table.engine.calls == []

    async def test_a_broken_speaker_does_not_reach_the_application(self) -> None:
        sink = RecordingSink(error=RuntimeError("the speaker went away"))
        async with Table(sink=sink) as table:
            await table.hit()
            await table.settled()

            outcomes = [event.outcome for event in table.of(SpeechCompleted)]
            assert outcomes == [SpeechOutcome.FAILED]
            assert table.of(AnalysisCompleted)


class TestTwoShotsInARow:
    async def test_shots_arriving_faster_than_they_can_be_said_never_overlap(
        self,
    ) -> None:
        """Three swings inside one sentence. Coaching drops what has gone stale
        and speech says one thing at a time, so however the two policies
        interact, the room never hears two voices."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.hit()
            for _ in range(200):
                await asyncio.sleep(0)

            await table.hit()
            await table.hit()
            for _ in range(200):
                await asyncio.sleep(0)

            gate.set()
            await table.settled()

        assert len(table.sink.started) == len(table.sink.played)
        assert len(table.sink.played) <= len(table.of(AnalysisCompleted))
        assert table.speech.health().failed == 0

    async def test_a_newer_coaching_line_replaces_one_still_waiting(self) -> None:
        """A golfer who hits again before the last line was said has moved on,
        and a coach two swings behind is worse than one who says nothing."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.hit()
            for _ in range(300):
                await asyncio.sleep(0)
            assert len(table.sink.started) == 1

            await table.speech.say(Utterance(text="About the shot before last."))
            await table.speech.say(Utterance(text="About the shot just hit."))
            gate.set()
            await table.settled()

        outcomes = [event.outcome for event in table.of(SpeechCompleted)]
        assert SpeechOutcome.SUPERSEDED in outcomes
        spoken = [text for text, _ in table.engine.calls]
        assert "About the shot before last." not in spoken
        assert "About the shot just hit." in spoken

    async def test_two_shots_never_produce_two_voices_at_once(self) -> None:
        async with Table() as table:
            for _ in range(4):
                await table.hit()
                await table.settled()

        assert len(table.sink.played) == len(table.sink.started)


class TestSomethingMoreUrgent:
    async def test_a_high_priority_line_takes_the_room_from_coaching(self) -> None:
        """Nothing produces one of these yet. This is the seam a future warning
        uses, and the only thing it has to know about everything else being said
        is a number."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.hit()
            for _ in range(300):
                await asyncio.sleep(0)
            assert len(table.sink.started) == 1

            await table.speech.say(
                Utterance(
                    text="Mind the club behind you.",
                    priority=SpeechPriority.ALERT,
                    perishable=False,
                )
            )
            gate.set()
            await table.settled()

        assert table.sink.interrupted == 1
        outcomes = [event.outcome for event in table.of(SpeechCompleted)]
        assert outcomes == [SpeechOutcome.INTERRUPTED, SpeechOutcome.SPOKEN]
        assert table.engine.calls[-1][0] == "Mind the club behind you."


class TestShutdown:
    async def test_nothing_is_left_running_and_the_speaker_is_released(self) -> None:
        table = Table()
        async with table:
            await table.hit()
            await table.settled()

        assert table.sink.closed == 1
        assert table.speech.is_available is False
        assert table.speech.is_idle
