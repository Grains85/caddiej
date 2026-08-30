"""One event, four outputs, and then everything back to normal.

The units are covered elsewhere. This is about the wiring, and about the one
claim that cannot be checked anywhere else: that a single scoring event reaches
the floor, the voice, the lights and OBS from one table of judgements, and that
when it is over none of the four is still doing anything.

The path being checked:

    hole.completed
        -> one entry in the catalogue
        -> a word on the projected floor
        -> a line in the speech queue
        -> a dip on a lamp
        -> a source revealed in OBS
        -> the duration
        -> all four back exactly as they were

Real bus, real director, real floor state, real speech service, real lighting
service, real wire format. The two things that touch hardware are faked — the
speaker and the bridge — and OBS is a stub, because there is no OBS.
"""

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.events import SpeechCompleted, SpeechStarted
from caddiej.domain.scoring import HoleCompleted, HoleResult
from caddiej.domain.speech import SpeechPriority
from caddiej.effects.catalogue import EFFECTS
from caddiej.effects.channels import FloorChannel, LightsChannel, SpeechChannel
from caddiej.effects.director import EffectDirector
from caddiej.effects.obs import ObsChannel
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorPoint, FloorState, MarkerVariant, ShotMetrics
from caddiej.lighting.fakes import BrokenLights, RecordingLights, a_room
from caddiej.lighting.modulation import Modulation
from caddiej.lighting.service import LightingService
from caddiej.speech.fakes import FakeSynthesizer, RecordingSink
from caddiej.speech.service import SpeechService

pytestmark = pytest.mark.asyncio


@dataclass
class StubObs:
    """An OBS that records what it was asked. There is no OBS."""

    connects: int = 0
    disconnects: int = 0
    calls: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)

    async def connect(self) -> None:
        self.connects += 1

    async def set_text(self, source_name: str, text: str) -> None:
        self.captions.append(text)
        self.calls.append("caption" if text else "blank")

    async def set_scene_item_visibility(
        self, scene_name: str, source_name: str, *, visible: bool
    ) -> None:
        self.calls.append("show" if visible else "hide")

    async def disconnect(self) -> None:
        self.disconnects += 1


class Table:
    """Every output CaddieJ has, on one real bus, with nothing real behind them."""

    def __init__(
        self,
        lights: RecordingLights | BrokenLights | None = None,
        room: float = 1.0,
        max_seconds: float = 0.25,
        use_obs: bool = True,
    ) -> None:
        self.bus = AsyncEventBus()
        self.seen: list[Event] = []

        self.floor = FloorService()
        self.snapshots: list[str] = []
        self.floor.add_listener(self._serialise)

        self.sink = RecordingSink()
        self.speech = SpeechService(
            publish=self.bus.publish,
            synthesizer=FakeSynthesizer(),
            sink=self.sink,
        )

        self.lights = RecordingLights(room=a_room(room)) if lights is None else lights
        self.lighting = LightingService(
            lights=self.lights,  # type: ignore[arg-type]
            modulation=Modulation(minimum=0.4, maximum=1.0),
            # A test has no patience, and no bridge to protect.
            max_commands_per_second=100.0,
        )

        self.obs = StubObs()
        channels: list[object] = [
            FloorChannel(self.floor),
            SpeechChannel(self.speech),
            LightsChannel(self.lighting),
        ]
        if use_obs:
            channels.append(
                ObsChannel(
                    lambda: self.obs,  # type: ignore[arg-type,return-value]
                    scene="CaddieJ",
                    source="Celebration",
                    text_source="Caption",
                )
            )

        self.director = EffectDirector(
            channels=channels,  # type: ignore[arg-type]
            # Real durations are seconds long; nobody waits five of them for a
            # test about wiring.
            max_seconds=max_seconds,
        )

    async def __aenter__(self) -> "Table":
        for kind in (SpeechStarted, SpeechCompleted):
            self.bus.subscribe(kind, self._remember)
        await self.bus.start()
        await self.speech.open()
        self.speech.subscribe(self.bus)
        await self.lighting.open()
        self.lighting.subscribe(self.bus)
        self.director.subscribe(self.bus)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.director.aclose()
        await self.speech.aclose()
        await self.lighting.aclose()
        await self.bus.stop()

    def _serialise(self, state: FloorState) -> None:
        """Every state change, as a browser would actually receive it."""

        self.snapshots.append(snapshot_message(state).model_dump_json())

    async def _remember(self, event: Event) -> None:
        self.seen.append(event)

    def of[E: Event](self, kind: type[E]) -> list[E]:
        return [event for event in self.seen if isinstance(event, kind)]

    async def holes_out(self, result: HoleResult) -> None:
        await self.bus.publish(HoleCompleted(result=result, hole_number=7))
        await asyncio.sleep(0.02)

    async def settled(self) -> None:
        for _ in range(300):
            if self.director.is_idle and self.speech.is_idle:
                await asyncio.sleep(0.02)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the room never settled")


class TestOneEventReachesEverything:
    async def test_an_eagle_lands_on_all_four_outputs(self) -> None:
        async with Table() as table:
            await table.holes_out(HoleResult.EAGLE)

            flourish = table.floor.state.flourish
            assert flourish is not None
            assert flourish.cue == "eagle"
            assert flourish.label == "EAGLE"

            assert table.obs.calls[:2] == ["caption", "show"]
            assert table.obs.captions[0] == "EAGLE"

            await table.settled()

            assert table.lights.applied, "the room dipped"
            assert [event.text for event in table.of(SpeechStarted)] == ["Eagle."]

    async def test_the_voice_speaks_as_an_announcement(self) -> None:
        """The tier that outranks coaching, so a birdie cuts off a sentence about
        the last shot's club path."""

        async with Table() as table:
            await table.holes_out(HoleResult.BIRDIE)
            await table.settled()

            (started,) = table.of(SpeechStarted)
            assert started.priority is SpeechPriority.ANNOUNCEMENT

    async def test_a_par_is_seen_and_not_heard(self) -> None:
        """The most important entry in the catalogue. A room that comments on
        every hole is a room somebody mutes."""

        async with Table() as table:
            await table.holes_out(HoleResult.PAR)

            assert table.floor.state.flourish is not None
            await table.settled()

            assert table.of(SpeechStarted) == []
            assert table.lights.applied, "but the lights still acknowledged it"

    async def test_the_word_reaches_a_browser_as_json(self) -> None:
        """The floor channel writes state; the wire has to carry it. A
        celebration nobody can serialise is a celebration nobody sees."""

        async with Table() as table:
            await table.holes_out(HoleResult.HOLE_IN_ONE)
            await table.settled()

        payloads = [json.loads(raw)["state"]["flourish"] for raw in table.snapshots]
        shown = [payload for payload in payloads if payload is not None]

        assert shown[0] == {
            "cue": "hole_in_one",
            "label": "HOLE IN ONE",
            "intensity": 1.0,
            "seconds": EFFECTS[HoleResult.HOLE_IN_ONE].seconds,
        }
        assert payloads[-1] is None, "and it was taken away again"


class TestEverythingIsPutBack:
    async def test_all_four_outputs_return_to_normal(self) -> None:
        async with Table(room=0.8) as table:
            await table.holes_out(HoleResult.ALBATROSS)
            await table.settled()

            assert table.floor.state.flourish is None
            assert table.obs.calls[-2:] == ["hide", "blank"]

        assert table.lights.restored == [a_room(0.8)]
        assert table.director.health().resets == 1

    async def test_the_round_is_untouched_underneath(self) -> None:
        """A celebration is a layer. Whatever the shot put on the floor has to
        survive it, or every birdie would wipe the numbers off the surface."""

        async with Table() as table:
            table.floor.show_marker(FloorPoint(x=0.5, y=0.72), MarkerVariant.DRIVER)
            table.floor.set_metrics(
                ShotMetrics(ball_speed_mph=148.2, launch_angle_degrees=12.6)
            )
            before = table.floor.state

            await table.holes_out(HoleResult.EAGLE)
            await table.settled()

            assert table.floor.state.marker == before.marker
            assert table.floor.state.metrics == before.metrics

    async def test_shutting_down_mid_celebration_leaves_nothing_running(self) -> None:
        """The room has to be normal before the process is gone: after shutdown
        there is nothing left that knows how to make it so."""

        table = Table(room=0.9, max_seconds=30.0)
        async with table:
            await table.holes_out(HoleResult.HOLE_IN_ONE)
            assert table.floor.state.flourish is not None

        assert table.floor.state.flourish is None
        assert table.obs.calls[-2:] == ["hide", "blank"]
        assert table.obs.disconnects == 1
        assert table.lights.restored == [a_room(0.9)]

    async def test_a_rarer_hole_replaces_a_lesser_one_without_leaving_a_trace(
        self,
    ) -> None:
        async with Table(max_seconds=30.0) as table:
            await table.holes_out(HoleResult.BIRDIE)
            await table.holes_out(HoleResult.HOLE_IN_ONE)

            flourish = table.floor.state.flourish
            assert flourish is not None
            assert flourish.cue == "hole_in_one"

            await table.director.aclose()

            assert table.floor.state.flourish is None
            assert table.director.health().replaced == 1


class TestNothingHereCanBreakTheGolf:
    async def test_a_dead_bridge_still_leaves_a_word_on_the_floor(self) -> None:
        async with Table(lights=BrokenLights()) as table:
            await table.holes_out(HoleResult.EAGLE)

            assert table.floor.state.flourish is not None
            await table.settled()

            assert table.of(SpeechStarted), "and the voice still said it"

    async def test_the_shot_path_is_not_held_up_by_a_celebration(self) -> None:
        """A celebration that occupied the event path would delay the next
        shot's numbers, which matter far more than the word. Isolation means
        a sibling subscriber is not delayed; this still asserts the handler
        returned so the effect is running and the bus is free."""

        async with Table(max_seconds=30.0) as table:
            await table.bus.publish(HoleCompleted(result=HoleResult.HOLE_IN_ONE))
            # The handler returned, the effect is running, and the bus is free.
            await asyncio.sleep(0.02)
            assert table.director.is_playing

            table.floor.set_metrics(
                ShotMetrics(ball_speed_mph=61.7, launch_angle_degrees=28.1)
            )
            assert table.floor.state.metrics is not None

            await table.director.aclose()

    async def test_no_obs_at_all_changes_nothing_else(self) -> None:
        async with Table(use_obs=False) as table:
            await table.holes_out(HoleResult.BIRDIE)
            await table.settled()

            assert table.obs.calls == []
            assert table.director.health().failures == 0
            assert "obs" not in table.director.health().channels
