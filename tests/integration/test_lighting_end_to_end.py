"""A shot going all the way from the launch monitor to the lights.

The units are covered elsewhere. This is about the wiring, and specifically about
the one claim that cannot be checked anywhere else: that the brightness sent to a
lamp is derived from the *actual samples* of the voice rather than from a timer, a
guess, or a model's opinion about when a light should change.

The path being checked:

    a swing
        -> a deterministic diagnosis
        -> a provider
        -> analysis.completed
        -> a rendered voice
        -> speech.started, carrying the measured envelope
        -> a brightness on a lamp
        -> speech.completed
        -> the room exactly as it was found

Real bus, real coaching service, real speech service, real lighting service. The
three things that touch hardware are faked: the model that writes the words, the
speaker that plays them, and the bridge.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from caddiej.coaching.fakes import ScriptedProvider
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
from caddiej.lighting.fakes import BrokenLights, RecordingLights, a_room
from caddiej.lighting.modulation import Flourish, Modulation
from caddiej.lighting.service import LightingService
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


def a_shot_with_no_evidence() -> Shot:
    """A shot nothing rankable was measured about: no club data at all, and
    only the ball numbers that need a baseline no first shot has yet."""

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
            launch_direction_degrees=None,
        ),
        handedness=Handedness.RIGHT,
        club=Club(kind=ClubKind.DRIVER, vendor_code="D"),
    )


class Table:
    """Coaching, speech and lighting on one real bus, so a test can hit a ball."""

    def __init__(
        self,
        lights: RecordingLights | BrokenLights | None = None,
        modulation: Modulation | None = None,
        lighting_enabled: bool = True,
        realtime: bool = False,
    ) -> None:
        self.bus = AsyncEventBus()
        self.seen: list[Event] = []
        self.engine = FakeSynthesizer()
        # Playing takes time only where a test is about the shape of the
        # reaction; elsewhere it returns at once, so a test about wiring is not
        # also a test about the clock. Where it does, it runs several times
        # faster than life: the lights still get many ticks inside one sentence,
        # which is all the shape needs, and nobody waits two seconds for it.
        self.sink = RecordingSink(realtime=realtime, speed=4.0)
        self.lights = RecordingLights() if lights is None else lights

        self.coaching = AnalysisService(
            publish=self.bus.publish,
            provider=ScriptedProvider(),  # type: ignore[arg-type]
            # Nobody hits two balls inside eight seconds, but a test does.
            min_interval_seconds=0.0,
        )
        self.speech = SpeechService(
            publish=self.bus.publish,
            synthesizer=self.engine,
            sink=self.sink,
            cache=AudioCache(),
        )
        self.lighting = LightingService(
            lights=self.lights,  # type: ignore[arg-type]
            modulation=modulation or Modulation(minimum=0.4, maximum=1.0),
            enabled=lighting_enabled,
            # A test has no patience, and no bridge to protect.
            max_commands_per_second=100.0,
        )

    async def __aenter__(self) -> "Table":
        for kind in (ShotDiagnosed, AnalysisCompleted, SpeechStarted, SpeechCompleted):
            self.bus.subscribe(kind, self._remember)
        await self.bus.start()
        await self.coaching.open()
        self.coaching.subscribe(self.bus)
        await self.speech.open()
        self.speech.subscribe(self.bus)
        await self.lighting.open()
        self.lighting.subscribe(self.bus)
        return self

    async def __aexit__(self, *_: object) -> None:
        # The same order the application uses.
        await self.speech.aclose()
        await self.lighting.aclose()
        await self.coaching.aclose()
        await self.bus.stop()

    async def _remember(self, event: Event) -> None:
        self.seen.append(event)

    async def hit(self, shot: Shot | None = None) -> None:
        await self.bus.publish(ShotCompleted(shot=shot or a_shot()))

    async def settled(self, timeout: float = 5.0) -> None:
        """Let the bus, coaching, speech and the lights all come to rest."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._at_rest():
                await asyncio.sleep(0)
                if self._at_rest():
                    return
            await asyncio.sleep(0.002)
        raise AssertionError("the pipeline never came to rest")

    def _at_rest(self) -> bool:
        return self.bus.is_idle and self.speech.is_idle and self.lighting.is_idle

    def of[EventType: Event](self, kind: type[EventType]) -> list[EventType]:
        return [event for event in self.seen if isinstance(event, kind)]


class TestASwingBecomesLight:
    async def test_the_lights_react_while_the_coaching_line_is_spoken(self) -> None:
        async with Table() as table:
            await table.hit()
            await table.settled()

        assert table.of(SpeechStarted), "nothing was said"
        assert table.lights.applied, "the lights never moved"
        assert table.lights.restored, "the room was never given back"

    async def test_the_brightness_comes_from_the_measured_waveform(self) -> None:
        """The claim the whole feature rests on. The fake voice babbles in
        syllables, so a measurement of it has humps and gaps in it — and the
        brightness sent to the lamp has to have the same humps and gaps, because
        that is where it came from."""

        async with Table(
            modulation=Modulation(minimum=0.2, maximum=1.0), realtime=True
        ) as table:
            await table.hit()
            await table.settled()

        envelope = table.of(SpeechStarted)[0].envelope
        assert envelope is not None and not envelope.is_empty

        levels = table.lights.levels
        assert len(levels) > 4, "too few commands to show a shape"
        # Not flat: the room followed something, rather than dimming once and
        # sitting there.
        assert max(levels) - min(levels) > 0.1
        # And within the range the modulation allows, against a lamp at full.
        assert min(levels) >= 0.2
        assert max(levels) <= 1.0

    async def test_the_room_comes_back_to_exactly_what_it_was(self) -> None:
        room = a_room(0.61, 0.28)
        async with Table(lights=RecordingLights(room=room)) as table:
            await table.hit()
            await table.settled()

        assert table.lights.restored[-1] == room

    async def test_the_baseline_is_read_once_per_line_not_once_per_command(
        self,
    ) -> None:
        async with Table(realtime=True) as table:
            await table.hit()
            await table.settled()

        assert table.lights.snapshots == 1
        assert len(table.lights.applied) > table.lights.snapshots

    async def test_a_shot_nothing_was_measured_about_leaves_the_lights_alone(
        self,
    ) -> None:
        """Lighting follows speech, and speech follows the coached line. A shot
        nothing was measured about produces neither — which is now the only
        case that does not, since a good shot gets a reinforcement."""

        async with Table() as table:
            await table.hit(a_shot_with_no_evidence())
            await table.settled()

        assert table.of(SpeechStarted) == []
        assert table.lights.applied == []
        assert table.lights.snapshots == 0

    async def test_several_shots_in_a_row_leave_the_room_where_it_started(
        self,
    ) -> None:
        """The drift that would be nobody's fault and everybody's problem: a
        baseline re-read while the lights were dimmed would leave the room a
        little darker after every swing."""

        room = a_room(0.8)
        async with Table(lights=RecordingLights(room=room)) as table:
            for _ in range(3):
                await table.hit()
                await table.settled()

        assert table.lights.restored == [room, room, room]


class TestAGestureFromOutside:
    async def test_a_gesture_asked_for_directly_reaches_the_lights(self) -> None:
        """The seam the effects director drives. Nothing about golf crosses it:
        a depth and a duration go in, and the room comes back afterwards."""

        async with Table(lights=RecordingLights(room=a_room(0.9))) as table:
            assert await table.lighting.play(
                Flourish(target=0.5, seconds=0.4), reason="eagle"
            )
            await asyncio.sleep(0.1)
            assert table.lights.applied

        assert table.lights.restored == [a_room(0.9)]
        assert table.lighting.health().cues == 1


class TestNothingHereCanBreakTheGolf:
    async def test_a_dead_bridge_does_not_stop_the_shot_or_the_voice(self) -> None:
        async with Table(lights=BrokenLights()) as table:
            await table.hit()
            await table.settled()

            assert table.of(ShotDiagnosed)
            assert table.of(AnalysisCompleted)
            assert len(table.sink.played) == 1
            assert table.lighting.health().failures >= 1

    async def test_lighting_switched_off_changes_nothing_upstream(self) -> None:
        async with Table(lighting_enabled=False) as table:
            await table.hit()
            await table.settled()

            assert table.of(ShotDiagnosed)
            assert len(table.sink.played) == 1
            assert table.lights.opened == 0
            assert table.lights.applied == []

    async def test_a_slow_bridge_never_delays_a_shot(self) -> None:
        """The reason the handlers start a task and return. The bus dispatches
        one handler at a time, so a lighting handler that waited for a bridge
        would hold up every shot behind it."""

        async with Table(lights=RecordingLights(apply_delay=0.2)) as table:
            await table.hit()
            started = asyncio.get_running_loop().time()
            await table.hit()
            elapsed = asyncio.get_running_loop().time() - started

            assert elapsed < 0.1, "a bridge held up the shot path"
            await table.settled()

    async def test_the_lights_are_given_back_when_a_line_is_cut_short(self) -> None:
        from caddiej.domain.speech import SpeechPriority, Utterance

        async with Table(lights=RecordingLights(room=a_room(1.0))) as table:
            await table.hit()
            # Wait for the room to be following the voice, then take the speaker
            # away with something more urgent.
            for _ in range(400):
                await asyncio.sleep(0)
                if table.lights.applied:
                    break
            assert table.lights.applied

            await table.speech.say(
                Utterance(
                    text="Mind the club behind you.",
                    priority=SpeechPriority.ALERT,
                    perishable=False,
                )
            )
            await table.settled()

        assert table.lights.restored[-1] == a_room(1.0)
        assert table.lighting.health().restore_failures == 0


class TestShutdown:
    async def test_the_room_is_given_back_and_the_bridge_released(self) -> None:
        table = Table(lights=RecordingLights(room=a_room(0.55)))
        async with table:
            await table.hit()
            await table.settled()

        assert table.lights.restored == [a_room(0.55)]
        assert table.lights.closed == 1
        assert table.lighting.is_idle
        assert table.lighting.is_available is False

    async def test_shutting_down_mid_line_still_gives_the_room_back(self) -> None:
        table = Table(lights=RecordingLights(room=a_room(0.7)))
        await table.__aenter__()
        await table.hit()
        for _ in range(400):
            await asyncio.sleep(0)
            if table.lights.applied:
                break
        assert table.lights.applied, "the lights never started moving"

        await table.__aexit__()

        assert table.lights.restored == [a_room(0.7)]
