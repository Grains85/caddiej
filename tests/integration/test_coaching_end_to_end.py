"""A shot going all the way from the launch monitor to a line on the floor.

The units are covered elsewhere. This is about the wiring: a real event bus, the
real play flow, the real floor state and the real HUD payload, with only the two
things that leave the machine faked — the model and the camera.

The path being checked is the one the milestone describes:

    a swing, and a clip of it
        -> analysis.started
        -> a provider
        -> strict validation
        -> analysis.completed
        -> words on the floor, and a row in the database

Nothing here spends anything. The provider is scripted, which is also how the
floor gets reviewed on a machine with no credentials.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from caddiej.adapters.hud.protocol import snapshot_message
from caddiej.coaching.fakes import (
    GOOD_REPLY,
    BrokenProvider,
    ScriptedProvider,
    StillFrames,
)
from caddiej.coaching.provider import ProviderRefusedError
from caddiej.coaching.service import AnalysisService
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.coaching import AdviceStatus
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    BallCleared,
    BallDetected,
    ClubSelected,
    ShotDiagnosed,
    ShotIngested,
    SwingClipReady,
)
from caddiej.domain.media import ClipRef
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)
from caddiej.floor import FloorService, PhysicalPoint
from caddiej.floor.presenter import FloorPresenter
from caddiej.persistence.service import build_history_service

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)
TEE = PhysicalPoint(x_m=0.5, y_m=0.72)


class Table:
    """Everything wired together, so a test can play a shot and look."""

    def __init__(self) -> None:
        self.bus = AsyncEventBus()
        self.floor = FloorService()
        self.presenter = FloorPresenter(self.floor, tee=TEE)
        self.events: list[object] = []

    async def open(
        self,
        provider: object,
        frames: object | None = None,
        history: object | None = None,
        **options: object,
    ) -> None:
        self.history = history
        self.flow = PlayFlow(
            floor=self.presenter,
            publish=self.bus.publish,
            shot_display_seconds=30.0,
            advice_display_seconds=30.0,
        )
        defaults: dict[str, object] = {
            "min_interval_seconds": 0.0,
            "clip_wait_seconds": 0.5,
            "timeout_seconds": 2.0,
        }
        defaults.update(options)
        self.coaching = AnalysisService(
            publish=self.bus.publish,
            provider=provider,  # type: ignore[arg-type]
            frames=frames,  # type: ignore[arg-type]
            record=None if history is None else history.record_advice,
            **defaults,  # type: ignore[arg-type]
        )
        self.flow.subscribe(self.bus)
        self.coaching.subscribe(self.bus)
        if history is not None:
            history.subscribe(self.bus)
        self.bus.subscribe(ShotDiagnosed, self._remember)
        self.bus.subscribe(AnalysisStarted, self._remember)
        self.bus.subscribe(AnalysisCompleted, self._remember)
        self.bus.subscribe(AnalysisFailed, self._remember)
        await self.bus.start()
        await self.coaching.open()
        self.flow.start()

    async def _remember(self, event: object) -> None:
        self.events.append(event)

    async def aclose(self) -> None:
        # Coaching before the bus, as the application does it: a job still in
        # flight has an outcome to announce.
        await self.coaching.aclose()
        await self.flow.aclose()
        await self.bus.stop()

    async def quiet(self, timeout: float = 3.0) -> None:
        """Wait until coaching has finished with everything it was given."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.coaching.health().queued or self.coaching._jobs:
            if loop.time() >= deadline:
                raise AssertionError("coaching never finished")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    def seen[T](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]

    @property
    def advice(self) -> object | None:
        return self.floor.state.advice


@pytest.fixture
async def table() -> AsyncIterator[Table]:
    subject = Table()
    yield subject
    await subject.aclose()


def a_shot(face: float = 6.5) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="gspro_connect",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=1.0,
        ),
        ball=BallLaunch(
            speed_mph=148.2,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            carry_yards=265.0,
            total_spin_rpm=2480.0,
        ),
        club_delivery=ClubDelivery(
            speed_mph=112.4,
            path_degrees=-1.0,
            face_to_target_degrees=face,
            angle_of_attack_degrees=-1.0,
        ),
        handedness=Handedness.RIGHT,
    )


def a_clip(shot_id: UUID) -> ClipRef:
    return ClipRef(
        clip_id=uuid4(),
        shot_id=shot_id,
        camera_id="down_the_line",
        path=Path("clips/2026/04/01/swing.mp4"),
        recorded_at=NOW - timedelta(seconds=2.0),
        impact_at=NOW,
        impact_offset_seconds=2.0,
        duration_seconds=3.5,
        fps=120.0,
        width=1280,
        height=720,
    )


async def a_swing(table: Table, shot: Shot | None = None) -> Shot:
    """Play a shot the way a round really does: club, ball, strike."""

    shot = shot or a_shot()
    await table.bus.publish(
        ClubSelected(club=Club(kind=ClubKind.DRIVER, vendor_code="DR"))
    )
    await table.bus.publish(BallDetected())
    await table.bus.publish(ShotIngested(shot=shot))
    await table.bus.publish(BallCleared())
    return shot


# --- the whole path -------------------------------------------------------


async def test_a_swing_becomes_a_coached_line_on_the_floor(table: Table) -> None:
    provider = ScriptedProvider()
    await table.open(provider)

    await a_swing(table)
    await table.quiet()

    # Every step of the flow the milestone describes, in order.
    assert table.seen(ShotDiagnosed)
    assert table.seen(AnalysisStarted)
    assert table.seen(AnalysisCompleted)
    assert not table.seen(AnalysisFailed)

    advice = table.floor.state.advice
    assert advice is not None
    assert advice.text == "FACE OPEN"


async def test_the_floor_names_the_fault_before_the_model_has_answered(
    table: Table,
) -> None:
    # The reason the two tiers exist. A player should never be watching a blank
    # floor while a network call happens.
    provider = ScriptedProvider(delay=0.3)
    await table.open(provider, timeout_seconds=5.0)

    await a_swing(table)
    await asyncio.sleep(0.05)

    assert provider.calls <= 1
    advice = table.floor.state.advice
    assert advice is not None
    assert advice.text == "FACE OPEN"


async def test_the_coached_line_reaches_the_browser_payload(table: Table) -> None:
    # The last hop: the floor state has to survive being turned into the message
    # the overlay actually receives.
    await table.open(ScriptedProvider())

    await a_swing(table)
    await table.quiet()

    payload = snapshot_message(table.floor.state).model_dump(mode="json")

    assert payload["state"]["advice"]["text"] == "FACE OPEN"
    assert payload["state"]["advice"]["band"] in {"fine", "watch", "fault"}


async def test_frames_from_the_swing_go_with_the_question(table: Table) -> None:
    frames = StillFrames()
    provider = ScriptedProvider()
    await table.open(provider, frames=frames, send_frames=True, timeout_seconds=5.0)

    shot = await a_swing(table)
    await asyncio.sleep(0.02)
    await table.bus.publish(
        SwingClipReady(shot_id=shot.shot_id, clip=a_clip(shot.shot_id))
    )
    await table.quiet()

    assert frames.requests
    assert table.seen(AnalysisStarted)[0].frames > 0
    assert provider.requests[0].images


async def test_a_good_shot_is_left_alone_all_the_way_through(table: Table) -> None:
    provider = ScriptedProvider()
    await table.open(provider)

    await a_swing(table, a_shot(face=0.5))
    await table.quiet()

    assert provider.calls == 0
    assert table.floor.state.advice is None
    # The numbers are still there. It is the coaching that is silent.
    assert table.floor.state.metrics is not None


# --- when the model is not there ------------------------------------------


async def test_a_provider_that_fails_leaves_the_deterministic_line_up(
    table: Table,
) -> None:
    # The property the whole design rests on: nothing a model does, or fails to
    # do, can take the floor away from the player.
    await table.open(BrokenProvider(error=ProviderRefusedError("no such model")))

    await a_swing(table)
    await table.quiet()

    assert table.seen(AnalysisFailed)
    assert not table.seen(AnalysisCompleted)
    advice = table.floor.state.advice
    assert advice is not None
    assert advice.text == "FACE OPEN"


async def test_a_model_that_answers_badly_leaves_it_up_too(table: Table) -> None:
    await table.open(ScriptedProvider(replies=[{"focus": "face_control"}]))

    await a_swing(table)
    await table.quiet()

    assert table.seen(AnalysisFailed)
    advice = table.floor.state.advice
    assert advice is not None
    assert advice.text == "FACE OPEN"


async def test_with_no_provider_the_round_plays_exactly_the_same(
    table: Table,
) -> None:
    # A fresh checkout with no credentials anywhere. This is the default.
    await table.open(provider=None)

    await a_swing(table)
    await table.quiet()

    assert not table.seen(AnalysisStarted)
    advice = table.floor.state.advice
    assert advice is not None
    assert advice.text == "FACE OPEN"


# --- and it is written down -----------------------------------------------


async def test_the_line_and_the_shot_end_up_in_the_same_database(
    table: Table, tmp_path: Path
) -> None:
    history = build_history_service(
        enabled=True, path=tmp_path / "history.sqlite3", player_name="Johan"
    )
    await history.open()
    try:
        await table.open(ScriptedProvider(), history=history)

        shot = await a_swing(table)
        await table.quiet()
        await asyncio.sleep(0.1)

        [stored] = await history.advice(shot.shot_id)
        assert stored.outcome.status is AdviceStatus.OK
        assert stored.outcome.advice.speech == GOOD_REPLY["speech"]
        assert stored.outcome.model == "scripted-1"
        assert await history.focus_counts() == {"face_control": 1}
    finally:
        await history.aclose()


async def test_a_failed_analysis_is_written_down_as_a_failure(
    table: Table, tmp_path: Path
) -> None:
    history = build_history_service(enabled=True, path=tmp_path / "history.sqlite3")
    await history.open()
    try:
        await table.open(
            BrokenProvider(error=ProviderRefusedError("no such model")), history=history
        )

        shot = await a_swing(table)
        await table.quiet()
        await asyncio.sleep(0.1)

        [stored] = await history.advice(shot.shot_id)
        assert stored.outcome.status is AdviceStatus.UNAVAILABLE
        assert stored.outcome.failure is not None
    finally:
        await history.aclose()
