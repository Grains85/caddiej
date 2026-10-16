"""Briefly showing an existing OBS source after a shot, and never costing one.

Shot Video and Top Impact are the same mechanism pointed at two different ProTee
views, so the lifecycle is written once and parameterised over the feature. They
used to be two packages, two `TimedObsSource` subclasses and two near-identical
test files; the subclasses differed in a string.

Four subjects, and the last two are the ones that matter on hardware:

* `TestTheRevealLifecycle` — hide at startup, show after the delay, hide after
  the duration, newest strike wins, and every OBS failure ends in golf
  continuing.
* `TestTheRevealStartsAtImpact` — what it waits on. Measured in the bay on
  2026-09-16, ProTee's trigger and GSPro's completed shot were 3.1 s apart on a
  chip and about 6.4 s apart on a full shot, because currentRound.dat cannot be
  written until the ball has stopped. A swing replay scheduled from the later of
  those starts after the player has picked the ball up.
* `TestTheTwoRevealsAreIndependent` — a bay commonly has one of the two views
  captured and not the other, so neither may cost the other a reveal.
* `TestTheFloorIsNeverWaiting` — a real bus, a real `PlayFlow` and a real
  presenter, with ten second delays against an OBS that never answers, and the
  shot result on the floor anyway. A result is a number a player can see; a
  revealed source is decoration.

Everything runs on a fake clock, so no test waits a production delay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from time import monotonic

import pytest

from caddiej.adapters.gspro.round_parse import parse_round_document
from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.adapters.obs import (
    FloorReveals,
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
    ObsVersionInfo,
)
from caddiej.core.delivery import Delivery
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.event_policy import delivery_for, queue_size_for
from caddiej.domain.events import RoundShotObserved, ShotDetected
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import PlayFlow
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.obs_reveal import (
    ObsSourceProbe,
    RevealFeature,
    TimedObsSource,
    probe_obs_source,
)

SCENE = "CaddieJ Test"
SOURCES = {
    RevealFeature.SHOT_VIDEO: "Shot Video",
    RevealFeature.TOP_IMPACT: "Top Impact",
}
FEATURES = tuple(SOURCES)

# The values a real currentRound.dat carried on the hitting bay.
REAL_BALL_SPEED = 32.83863067626953
REAL_TOTAL_DISTANCE = 28.0288715
REAL_DISTANCE_TO_PIN = 9.007874


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(
        shot_id=shot_id,
        ball_speed_raw=REAL_BALL_SPEED,
        total_distance_raw=REAL_TOTAL_DISTANCE,
        distance_to_pin_raw=REAL_DISTANCE_TO_PIN,
    )


def a_result(shot_id: str) -> RoundShotObserved:
    """GSPro's completed shot. Seconds late on real hardware."""

    return RoundShotObserved(shot=a_round(shot_id))


def an_impact(trigger_id: str) -> ShotDetected:
    """The launch monitor's own trigger, which is what a reveal waits on."""

    return ShotDetected(trigger_id=trigger_id, source="protee_log")


def a_presenter(floor: FloorService) -> FloorPresenter:
    return FloorPresenter(floor, layout_from_tees(), club_path_length=0.4)


async def _ignore(_event: object) -> None:
    return None


@dataclass
class StubClient:
    """An OBS that records what it was asked and can be made to refuse or hang."""

    connect_error: BaseException | None = None
    command_error: BaseException | None = None
    hang: asyncio.Event | None = None

    connects: int = 0
    disconnects: int = 0
    visibility: list[tuple[str, str, bool]] = field(default_factory=list)

    async def connect(self) -> None:
        self.connects += 1
        if self.hang is not None:
            await self.hang.wait()
        if self.connect_error is not None:
            raise self.connect_error

    async def scene_item_id(self, scene_name: str, source_name: str) -> int:
        if self.command_error is not None:
            raise self.command_error
        return 7

    async def set_scene_item_visibility(
        self, scene_name: str, source_name: str, *, visible: bool
    ) -> None:
        if self.command_error is not None:
            raise self.command_error
        self.visibility.append((scene_name, source_name, visible))

    async def disconnect(self) -> None:
        self.disconnects += 1


class Clock:
    """Release sleeps one at a time, so a 4 s duration costs a test nothing."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        gate = asyncio.Event()
        self._gates.append(gate)
        try:
            await gate.wait()
        finally:
            if gate in self._gates:
                self._gates.remove(gate)

    @property
    def waiting(self) -> int:
        return len(self._gates)

    async def wait_slept(self, count: int) -> None:
        for _ in range(100):
            if len(self.slept) >= count and self._gates:
                return
            await asyncio.sleep(0)
        raise AssertionError(
            f"expected {count} sleeps with a waiter; slept={self.slept}, "
            f"waiting={len(self._gates)}"
        )

    async def wait_pending(self) -> None:
        await self.wait_slept(1)

    async def release_one(self) -> None:
        await self.wait_pending()
        self._gates[0].set()
        # Twice: once to wake the sleeper, once to let what follows it run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class Table:
    """One reveal, a fake clock, and every OBS client it opened."""

    def __init__(
        self,
        feature: RevealFeature = RevealFeature.SHOT_VIDEO,
        template: StubClient | None = None,
        *,
        delay_seconds: float = 0.05,
        duration_seconds: float = 0.1,
    ) -> None:
        self.feature = feature
        self.source = SOURCES[feature]
        self.clients: list[StubClient] = []
        self.template = template or StubClient()
        self.clock = Clock()
        self.floor_reveals = FloorReveals()
        self.reveal = TimedObsSource(
            self._make,
            SCENE,
            self.source,
            feature=feature,
            enabled=True,
            delay_seconds=delay_seconds,
            duration_seconds=duration_seconds,
            command_timeout=0.5,
            shutdown_timeout=0.5,
            sleep=self.clock.sleep,
            floor_reveals=self.floor_reveals,
        )

    def _make(self) -> StubClient:
        client = StubClient(
            connect_error=self.template.connect_error,
            command_error=self.template.command_error,
            hang=self.template.hang,
        )
        self.clients.append(client)
        return client

    async def start(self) -> None:
        """Start up and wait for the source to have been hidden."""

        startup = self.reveal.start()
        assert startup is not None
        await startup

    @property
    def client(self) -> StubClient:
        return self.clients[-1]

    @property
    def visibility(self) -> list[tuple[str, str, bool]]:
        shown: list[tuple[str, str, bool]] = []
        for client in self.clients:
            shown.extend(client.visibility)
        return shown

    def shown(self) -> list[bool]:
        return [visible for _, _, visible in self.visibility]


@pytest.mark.parametrize("feature", FEATURES)
class TestTheRevealLifecycle:
    async def test_startup_hides_the_source(self, feature: RevealFeature) -> None:
        """OBS writes visibility into the scene collection, so it remembers
        whatever was showing when it last stopped."""

        table = Table(feature)

        await table.start()

        assert table.visibility == [(SCENE, SOURCES[feature], False)]

    async def test_a_shot_does_not_show_the_source_immediately(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()
        table.client.visibility.clear()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.wait_pending()

        assert True not in table.shown(), "the delay is what makes it a reveal"

    async def test_the_source_becomes_visible_after_the_delay(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()
        table.client.visibility.clear()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()

        assert table.shown()[-1] is True
        assert table.clock.slept[0] == 0.05

    async def test_the_source_is_hidden_after_the_duration(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()
        await table.clock.release_one()

        assert table.shown()[-1] is False
        assert table.clock.slept == [0.05, 0.1]
        assert table.clock.waiting == 0

    async def test_only_the_configured_source_is_touched(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()

        assert {name for _scene, name, _v in table.visibility} == {SOURCES[feature]}
        assert {scene for scene, _name, _v in table.visibility} == {SCENE}

    async def test_every_toggle_is_declared_so_capture_can_tell_it_from_a_leak(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()
        mark = table.floor_reveals.mark()
        assert table.floor_reveals.since(mark, SCENE) == frozenset()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()

        assert table.shown()[-1] is True
        assert table.floor_reveals.since(mark, SCENE) == {SOURCES[feature]}
        assert table.floor_reveals.since(mark, "CaddieJ Capture") == frozenset()

    async def test_a_newer_strike_replaces_the_previous_reveal(
        self, feature: RevealFeature
    ) -> None:
        """The reason each reveal carries a generation.

        Trigger 16's hide is still in flight when 17 lands. If it were allowed
        to run, the player would watch their own shot disappear a moment after
        it appeared.
        """

        table = Table(feature)
        await table.start()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()
        assert table.shown()[-1] is True

        await table.reveal.on_shot_detected(an_impact("17"))
        await table.clock.wait_slept(3)
        # Hidden first, deliberately: that is the wait for ProTee to redraw.
        assert table.shown()[-1] is False

        await table.clock.release_one()
        assert table.shown()[-1] is True

        await table.clock.release_one()
        assert table.shown()[-1] is False
        assert table.clock.waiting == 0

    async def test_a_repeated_trigger_id_does_not_start_a_second_reveal(
        self, feature: RevealFeature
    ) -> None:
        # A log the observer had to re-read from the start replays its detections.
        table = Table(feature)
        await table.start()

        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.wait_pending()
        await table.reveal.on_shot_detected(an_impact("16"))
        await asyncio.sleep(0)

        assert table.clock.slept == [0.05]

    async def test_shutdown_cancels_a_pending_reveal_and_leaves_it_hidden(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()
        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.wait_pending()

        await table.reveal.aclose()

        assert table.shown()[-1] is False
        assert table.client.disconnects == 1
        assert table.clock.waiting == 0

    async def test_an_obs_that_cannot_be_reached_changes_nothing_else(
        self, feature: RevealFeature
    ) -> None:
        table = Table(
            feature, StubClient(connect_error=ObsTransportConnectionError(""))
        )

        await table.start()
        await table.reveal.on_shot_detected(an_impact("16"))
        await table.clock.release_one()
        await table.clock.release_one()
        await table.reveal.aclose()

        assert table.shown() == []

    async def test_a_missing_source_is_named_once_and_golf_continues(
        self, feature: RevealFeature, caplog: pytest.LogCaptureFixture
    ) -> None:
        table = Table(feature, StubClient(command_error=ObsResourceNotFoundError("no")))

        with caplog.at_level(logging.WARNING):
            await table.start()
            await table.reveal.on_shot_detected(an_impact("16"))
            await table.clock.release_one()
            await table.reveal.aclose()

        missing = [
            record
            for record in caplog.records
            if record.message == f"{feature.value}_source_missing"
        ]
        assert len(missing) == 1, "a name that does not match will not match later"
        assert table.shown() == []

    async def test_disabled_never_talks_to_obs(self, feature: RevealFeature) -> None:
        def boom() -> StubClient:
            raise AssertionError("OBS must not be contacted when disabled")

        reveal = TimedObsSource(
            boom, SCENE, SOURCES[feature], feature=feature, enabled=False
        )
        reveal.subscribe(AsyncEventBus())

        assert reveal.start() is None
        await reveal.on_shot_detected(an_impact("16"))
        await reveal.aclose()


@pytest.mark.parametrize("feature", FEATURES)
class TestTheRevealStartsAtImpact:
    """What a reveal waits on, which is the whole point of the feature."""

    async def test_the_reveal_is_scheduled_from_the_launch_monitor_detection(
        self, feature: RevealFeature
    ) -> None:
        table = Table(feature)
        await table.start()
        table.client.visibility.clear()
        bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
        table.reveal.subscribe(bus)
        await bus.start()
        try:
            await bus.publish(an_impact("16"))
            assert await bus.drain(timeout=2.0)
            await table.clock.release_one()
        finally:
            await bus.stop(timeout=1.0)

        assert table.shown()[-1] is True
        assert table.clock.slept[0] == 0.05

    async def test_gspro_s_completed_shot_no_longer_starts_a_reveal(
        self, feature: RevealFeature
    ) -> None:
        """The detection, and only that. Otherwise one strike would show the
        source twice: once at impact and once when the ball stopped."""

        table = Table(feature)
        await table.start()
        table.client.visibility.clear()
        bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
        table.reveal.subscribe(bus)
        await bus.start()
        try:
            await bus.publish(a_result("A"))
            assert await bus.drain(timeout=2.0)
            await asyncio.sleep(0)
        finally:
            await bus.stop(timeout=1.0)

        assert table.clock.slept == []
        assert table.shown() == []

    async def test_a_detection_the_bus_cannot_deliver_is_dropped_not_queued(
        self, feature: RevealFeature
    ) -> None:
        """The publisher is the ProTee observer, which also feeds the address HUD
        and the floor's readiness. It may never be made to wait on decoration."""

        assert delivery_for(feature.value, ShotDetected) is Delivery.DROP

    async def test_the_log_event_names_and_the_feature_field_agree(
        self, feature: RevealFeature, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two reveals write the same fields, so the events have to say which.

        An operator watching a bay wants both questions answerable from one
        log: "did anything get revealed for that shot" and "did Top Impact".
        """

        table = Table(feature)

        with caplog.at_level(logging.INFO):
            await table.start()
            await table.reveal.on_shot_detected(an_impact("16"))
            await table.clock.release_one()

        named = [
            record
            for record in caplog.records
            if record.message.startswith(f"{feature.value}_")
        ]
        assert feature.scheduled_event in {record.message for record in named}
        assert f"{feature.value}_source_visible" in {record.message for record in named}
        assert {record.obs_feature for record in named} == {feature.value}

        other = next(f for f in FEATURES if f is not feature)
        assert not [
            record
            for record in caplog.records
            if record.message.startswith(f"{other.value}_")
        ]


class TestTheTwoRevealsAreIndependent:
    """One strike, two sources, and no way for either to spoil the other.

    They are separate instances with separate connections, locks and counters,
    which is what makes each of the failures below local to one of them.
    """

    def pair(
        self,
        *,
        video_template: StubClient | None = None,
        impact_template: StubClient | None = None,
    ) -> tuple[AsyncEventBus, Table, Table]:
        bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
        video = Table(
            RevealFeature.SHOT_VIDEO,
            video_template,
            delay_seconds=0.75,
            duration_seconds=4.0,
        )
        impact = Table(
            RevealFeature.TOP_IMPACT,
            impact_template,
            delay_seconds=1.5,
            duration_seconds=4.0,
        )
        video.reveal.subscribe(bus)
        impact.reveal.subscribe(bus)
        return bus, video, impact

    async def strike(self, bus: AsyncEventBus, *tables: Table) -> None:
        for table in tables:
            table.reveal.start()
        await bus.start()
        try:
            await bus.publish(an_impact("16"))
            assert await bus.drain(timeout=2.0)
            for table in tables:
                if table.template.hang is None:
                    await table.clock.release_one()
        finally:
            for table in tables:
                await table.reveal.aclose()
            await bus.stop(timeout=1.0)

    async def test_one_strike_reveals_both_sources(self) -> None:
        bus, video, impact = self.pair()

        await self.strike(bus, video, impact)

        assert (SCENE, SOURCES[RevealFeature.SHOT_VIDEO], True) in video.visibility
        assert (SCENE, SOURCES[RevealFeature.TOP_IMPACT], True) in impact.visibility
        # Each waited its own configured delay, not a shared one.
        assert video.clock.slept[0] == 0.75
        assert impact.clock.slept[0] == 1.5

    async def test_a_missing_top_impact_source_still_reveals_shot_video(self) -> None:
        bus, video, impact = self.pair(
            impact_template=StubClient(command_error=ObsResourceNotFoundError("600"))
        )

        await self.strike(bus, video, impact)

        assert impact.shown() == []
        assert True in video.shown()

    async def test_an_obs_that_never_answers_shot_video_still_reveals_top_impact(
        self,
    ) -> None:
        bus, video, impact = self.pair(video_template=StubClient(hang=asyncio.Event()))

        await self.strike(bus, video, impact)

        assert (SCENE, SOURCES[RevealFeature.TOP_IMPACT], True) in impact.visibility
        assert video.shown() == []


class TestTheFloorIsNeverWaiting:
    """The hard requirement: a reveal cannot delay a shot result.

    Real bus, real `PlayFlow`, real presenter — because the delay this is about
    would come from the wiring between them, and a test that stubs the wiring
    cannot see it. The reveals are configured with ten second delays throughout
    and no test here waits any of them.
    """

    def stack(
        self, template: StubClient | None = None
    ) -> tuple[AsyncEventBus, FloorService, list[TimedObsSource], list[StubClient]]:
        bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
        floor = FloorService()
        play = PlayFlow(
            floor=a_presenter(floor),
            publish=bus.publish,
            shot_result_min_duration=8.0,
            shot_result_max_duration=30.0,
            sleep=Clock().sleep,
        )
        play.subscribe(bus)

        clients: list[StubClient] = []
        settings = template or StubClient()

        def make() -> StubClient:
            client = StubClient(
                connect_error=settings.connect_error,
                command_error=settings.command_error,
                hang=settings.hang,
            )
            clients.append(client)
            return client

        reveals = [
            TimedObsSource(
                make,
                SCENE,
                SOURCES[feature],
                feature=feature,
                enabled=True,
                # Far longer than any test here is allowed to take.
                delay_seconds=10.0,
                duration_seconds=10.0,
                command_timeout=10.0,
                # Shutdown must not wait on OBS either; a hung socket is the case.
                shutdown_timeout=0.2,
            )
            for feature in FEATURES
        ]
        for reveal in reveals:
            reveal.subscribe(bus)
        return bus, floor, reveals, clients

    async def result_after_a_shot(
        self, template: StubClient | None = None
    ) -> tuple[dict, float]:
        """Play one whole shot, then read the browser-facing snapshot and the cost.

        Both events, in the order the room produces them: the detection that puts
        two ten second reveals in flight, and then the result the player is
        waiting to read. Both reveals are still running when the snapshot is
        taken.
        """

        bus, floor, reveals, _clients = self.stack(template)
        await bus.start()
        for reveal in reveals:
            reveal.start()
        started = monotonic()
        try:
            await bus.publish(an_impact("16"))
            await bus.publish(a_result("A"))
            assert await bus.drain(timeout=2.0)
            elapsed = monotonic() - started
            payload = snapshot_message(floor.state).model_dump(mode="json")
        finally:
            # Cancels the ten second reveals rather than waiting for them.
            for reveal in reveals:
                await reveal.aclose()
            await bus.stop(timeout=1.0)
        return payload["state"], elapsed

    async def test_the_result_is_on_the_floor_before_a_ten_second_delay_elapses(
        self,
    ) -> None:
        state, elapsed = await self.result_after_a_shot()

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert state["round_result"]["shot_distance_display"] == "28 m"
        assert elapsed < 1.0, f"the result waited {elapsed:.2f}s on the reveals"

    async def test_an_obs_that_never_answers_cannot_hold_up_the_result(self) -> None:
        # A connection OBS accepts and then does nothing with is the worst case:
        # it fails no faster than the timeout, which is longer than a golf shot.
        hanging = StubClient(hang=asyncio.Event())

        state, elapsed = await self.result_after_a_shot(hanging)

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert elapsed < 1.0

    async def test_a_missing_obs_cannot_hold_up_the_result(self) -> None:
        refusing = StubClient(connect_error=ObsTransportConnectionError("obs down"))

        state, elapsed = await self.result_after_a_shot(refusing)

        assert state["round_result"]["shot_distance_display"] == "28 m"
        assert elapsed < 1.0

    async def test_a_missing_source_cannot_hold_up_the_result(self) -> None:
        absent = StubClient(command_error=ObsResourceNotFoundError("no such source"))

        state, elapsed = await self.result_after_a_shot(absent)

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert elapsed < 1.0

    async def test_an_obs_call_that_fails_cannot_hold_up_the_result(self) -> None:
        broken = StubClient(command_error=RuntimeError("obs said no"))

        state, elapsed = await self.result_after_a_shot(broken)

        assert state["round_result"]["ball_speed_display"] == "118 km/h"
        assert elapsed < 1.0

    async def test_the_browser_is_given_no_raw_float_to_fall_back_on(self) -> None:
        state, _elapsed = await self.result_after_a_shot()
        panel = state["round_result"]

        assert "ball_speed_raw" not in panel
        assert "total_distance_raw" not in panel
        assert str(REAL_BALL_SPEED) not in str(state)
        assert str(REAL_TOTAL_DISTANCE) not in str(state)


async def test_a_real_currentround_document_reaches_the_browser_as_km_h_and_metres() -> (
    None
):
    """The whole trace in one test, because every stage of it has been suspected.

    currentRound.dat text → ObservedRoundShot → RoundShotObserved → PlayFlow →
    FloorPresenter → RoundResultView → FloorState → the JSON `/snapshot` and the
    WebSocket both carry. The numbers are the ones the hitting bay produced.
    """

    document = json.dumps(
        [
            {
                "ShotID": "1f0c",
                "PlayerName": "Johan",
                "Hole": 1,
                "HoleShot": 1,
                "BallSpeed": REAL_BALL_SPEED,
                "TotalDistance": REAL_TOTAL_DISTANCE,
                "DistanceToPin": REAL_DISTANCE_TO_PIN,
                "StartingSurface": 1,
                "EndingSurface": 2,
            }
        ]
    )

    shots = parse_round_document(document)
    assert shots is not None
    observed = shots[0]
    assert observed.ball_speed_raw == REAL_BALL_SPEED
    assert observed.total_distance_raw == REAL_TOTAL_DISTANCE
    assert observed.distance_to_pin_raw == REAL_DISTANCE_TO_PIN

    floor = FloorService()
    play = PlayFlow(
        floor=a_presenter(floor),
        publish=_ignore,
        shot_result_min_duration=8.0,
        shot_result_max_duration=30.0,
        sleep=Clock().sleep,
    )

    await play.on_round_shot(RoundShotObserved(shot=observed))

    view = floor.state.round_result
    assert view is not None
    assert view.ball_speed_display == "118 km/h"
    assert view.shot_distance_display == "28 m"
    assert view.remaining_distance_display == "9 m"
    # The raw values stay in the domain, unrounded, under GSPro's own names.
    assert view.ball_speed_raw == REAL_BALL_SPEED
    assert view.total_distance_raw == REAL_TOTAL_DISTANCE
    assert view.distance_to_pin_raw == REAL_DISTANCE_TO_PIN

    wire = snapshot_message(floor.state).model_dump_json()
    panel = json.loads(wire)["state"]["round_result"]
    assert panel["ball_speed_display"] == "118 km/h"
    assert panel["shot_distance_display"] == "28 m"
    assert panel["remaining_distance_display"] == "9 m"
    assert "ball_speed_raw" not in panel
    assert "distance_to_pin_raw" not in panel
    assert str(REAL_BALL_SPEED) not in wire


class ProbeClient(StubClient):
    """The stub, plus the version read the diagnostic prints."""

    async def get_version(self) -> ObsVersionInfo:
        return ObsVersionInfo(
            obs_version="30.2.3", websocket_version="5.5.2", rpc_version=1
        )


@pytest.mark.parametrize("feature", FEATURES)
class TestTheRealObsDiagnostic:
    """`caddiej --check-obs-shot-video` / `--check-obs-top-impact`.

    Either CaddieJ cannot control the source, or it can and the shot never
    reached the reveal. Those look the same from the bay.
    """

    async def probe(
        self, feature: RevealFeature, client: ProbeClient
    ) -> ObsSourceProbe:
        return await probe_obs_source(
            feature=feature,
            host="127.0.0.1",
            port=4455,
            password="not-printed",
            connect_timeout=1.0,
            scene=SCENE,
            source=SOURCES[feature],
            hold_seconds=0.0,
            client_factory=lambda: client,
        )

    async def test_it_shows_the_source_and_puts_it_back(
        self, feature: RevealFeature
    ) -> None:
        client = ProbeClient()

        probe = await self.probe(feature, client)

        source = SOURCES[feature]
        assert probe.ok
        assert client.visibility == [
            (SCENE, source, True),
            (SCENE, source, False),
        ]
        assert client.disconnects == 1
        assert "scene item id 7" in probe.report
        assert feature.label in probe.report
        assert feature.scheduled_event in probe.report

    async def test_a_missing_source_names_the_scene_and_the_source(
        self, feature: RevealFeature
    ) -> None:
        client = ProbeClient(command_error=ObsResourceNotFoundError("600"))

        probe = await self.probe(feature, client)

        assert not probe.ok
        assert "FAILED at GetSceneItemId" in probe.report
        assert SOURCES[feature] in probe.report and SCENE in probe.report
        assert client.visibility == []

    async def test_an_unreachable_obs_points_at_the_websocket_settings(
        self, feature: RevealFeature
    ) -> None:
        client = ProbeClient(connect_error=ObsTransportConnectionError("refused"))

        probe = await self.probe(feature, client)

        assert not probe.ok
        assert "FAILED at connect" in probe.report
        assert "WebSocket Server Settings" in probe.report

    async def test_the_password_is_never_in_the_report(
        self, feature: RevealFeature
    ) -> None:
        for client in (
            ProbeClient(),
            ProbeClient(connect_error=ObsTransportConnectionError("refused")),
            ProbeClient(command_error=ObsResourceNotFoundError("600")),
        ):
            probe = await self.probe(feature, client)

            assert "not-printed" not in probe.report
