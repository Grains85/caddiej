"""The simulated round, from bytes on a socket to the floor and the database.

This is the only test that exercises the whole path at once: a fake device writes
Open Connect JSON to a real socket, the real adapter frames and interprets it, the
real event bus dispatches it, the real domain decides what it means, and the real
floor state and the real SQLite file come out the other end.

It binds loopback ports and writes a database under `tmp_path`, so it is gated
like the other integration tests. No external software is involved.
"""

import asyncio
import os
from pathlib import Path

import pytest

from caddiej.adapters.gspro.simulation import ScriptedShot, run_simulated_round
from caddiej.config import Settings
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import ClubKind
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorPoint, FloorState, MarkerVariant
from caddiej.persistence import HistoryService, build_history_service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to bind a real loopback port",
    ),
]

TEE = FloorPoint(x=0.5, y=0.72)


class Round:
    """A whole CaddieJ, minus the web server, playing against itself."""

    def __init__(
        self,
        shot_display_seconds: float = 0.1,
        database: Path | None = None,
        record_raw_payloads: bool = False,
    ) -> None:
        # The whole round is compressed, but the shot display still has to be
        # shorter than the gap between shots, or the floor would never get back
        # to an address between them — which is true of the real timings too.
        self.settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            GSPRO_MODE="simulate",
            GSPRO_BIND_PORT=0,
            GSPRO_SIMULATION_STEP_SECONDS=0.2,
            GSPRO_SHOT_ASSEMBLY_WINDOW=0.02,
            GSPRO_BALL_PRESENT_DEBOUNCE=0.0,
            GSPRO_BALL_ABSENT_DEBOUNCE=0.0,
            HISTORY_ENABLED=database is not None,
            HISTORY_DATABASE_PATH=str(database or "unused.sqlite3"),
            HISTORY_RECORD_RAW_PAYLOADS=record_raw_payloads,
            PLAYER_NAME="Johan",
        )
        self.floor = FloorService()
        self.states: list[FloorState] = []
        self.floor.add_listener(self.states.append)
        self.bus = AsyncEventBus()
        self.flow = PlayFlow(
            floor=FloorPresenter(self.floor, tee=TEE),
            publish=self.bus.publish,
            shot_display_seconds=shot_display_seconds,
        )
        self.flow.subscribe(self.bus)
        self.history: HistoryService = build_history_service(
            enabled=self.settings.history_enabled,
            path=self.settings.history_database_path,
            player_name=self.settings.player_name,
        )
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Round":
        await self.history.open()
        self.history.subscribe(self.bus)
        await self.bus.start()
        self._task = asyncio.create_task(
            run_simulated_round(self.settings, self.bus.publish),
            name="simulated-round",
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.flow.aclose()
        await self.bus.stop()
        await self.history.aclose()

    async def wait_until(self, predicate, timeout: float = 15.0) -> FloorState:
        """Wait for a floor state satisfying `predicate`, or say what was seen."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for state in tuple(self.states):
                if predicate(state):
                    return state
            if loop.time() >= deadline:
                raise AssertionError(
                    f"no matching floor state in {len(self.states)} versions"
                )
            await asyncio.sleep(0.02)

    async def wait_for_shots(self, count: int, timeout: float = 20.0) -> None:
        """Wait until the database has been told about `count` shots."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.history.shots_recorded < count:
            if loop.time() >= deadline:
                raise AssertionError(
                    f"only {self.history.shots_recorded} of {count} shots "
                    "reached the database"
                )
            await asyncio.sleep(0.02)


async def test_the_simulated_round_puts_a_driver_marker_on_the_floor() -> None:
    async with Round() as round_:
        state = await round_.wait_until(
            lambda s: s.marker.visible and s.marker.variant is MarkerVariant.DRIVER
        )

        assert state.marker.position == TEE


async def test_placing_a_ball_hides_the_marker() -> None:
    async with Round() as round_:
        await round_.wait_until(lambda s: s.marker.visible)

        await round_.wait_until(lambda s: not s.marker.visible)


async def test_a_simulated_shot_reaches_the_floor_as_a_path_and_numbers() -> None:
    async with Round() as round_:
        state = await round_.wait_until(
            lambda s: s.club_path is not None and s.metrics is not None
        )

        assert state.metrics is not None
        assert state.metrics.ball_speed_mph == pytest.approx(148.2)
        assert state.club_path is not None
        assert state.club_path.start == TEE


async def test_the_floor_returns_to_an_address_after_the_shot() -> None:
    async with Round() as round_:
        await round_.wait_until(lambda s: s.metrics is not None)

        await round_.wait_until(
            lambda s: s.metrics is None and s.club_path is None and s.marker.visible
        )


async def test_the_marker_follows_the_club_through_the_round() -> None:
    async with Round() as round_:
        await round_.wait_until(
            lambda s: s.marker.visible and s.marker.variant is MarkerVariant.DRIVER
        )
        await round_.wait_until(
            lambda s: s.marker.visible and s.marker.variant is MarkerVariant.NORMAL
        )
        await round_.wait_until(
            lambda s: s.marker.visible and s.marker.variant is MarkerVariant.PUTTER
        )


async def test_a_putt_shows_no_carry_because_none_was_measured() -> None:
    async with Round() as round_:
        putt = await round_.wait_until(
            lambda s: s.metrics is not None and s.metrics.ball_speed_mph < 20.0
        )

        assert putt.metrics is not None
        assert putt.metrics.carry_yards is None


async def test_the_scripted_round_covers_every_marker_variant() -> None:
    # A demo that never shows the putter marker cannot be used to check it.
    from caddiej.adapters.gspro.simulation import scripted_round

    codes = {shot.club_code for shot in scripted_round()}

    assert {"DR", "PT"} <= codes
    assert all(isinstance(shot, ScriptedShot) for shot in scripted_round())


# --- and into the database ------------------------------------------------


async def test_a_simulated_shot_is_stored_with_the_club_it_was_hit_with(
    tmp_path: Path,
) -> None:
    # The club is the thing the launch monitor never says, so a stored shot that
    # knows it is proof that the domain attributed it on the way past.
    async with Round(database=tmp_path / "history.sqlite3") as round_:
        await round_.wait_for_shots(1)

        stored = await round_.history.shots()
        first = stored[0]
        assert first.shot.club is not None
        assert first.shot.club.kind is ClubKind.DRIVER
        assert first.shot.club.vendor_code == "DR"
        assert first.player_name == "Johan"


async def test_a_stored_shot_keeps_the_numbers_the_device_reported(
    tmp_path: Path,
) -> None:
    async with Round(database=tmp_path / "history.sqlite3") as round_:
        await round_.wait_for_shots(1)

        first = (await round_.history.shots())[0]
        assert first.shot.ball is not None
        assert first.shot.ball.speed_mph == pytest.approx(148.2)
        assert first.shot.ball.carry_yards == pytest.approx(268.4)
        assert first.shot.club_delivery is not None
        assert first.shot.club_delivery.path_degrees == pytest.approx(-2.4)
        assert first.shot.source.kind == "gspro_connect"
        assert first.shot.source.device_id == "CaddieJ Simulated LM"


async def test_a_round_of_several_clubs_aggregates_per_club(
    tmp_path: Path,
) -> None:
    async with Round(database=tmp_path / "history.sqlite3") as round_:
        # A driver, an iron, a wedge and a putt.
        await round_.wait_for_shots(4)

        summary = await round_.history.analytics()

        assert summary is not None
        assert summary.shots >= 4
        clubs = {aggregate.club for aggregate in summary.by_club}
        assert {ClubKind.DRIVER, ClubKind.IRON, ClubKind.WEDGE, ClubKind.PUTTER} <= (
            clubs
        )
        assert summary.overall["ball_speed"].maximum == pytest.approx(148.2)
        assert summary.overall["ball_speed"].minimum == pytest.approx(6.2)


async def test_a_putt_is_stored_without_a_carry_it_never_had(
    tmp_path: Path,
) -> None:
    # A zero would be a number somebody might later average.
    async with Round(database=tmp_path / "history.sqlite3") as round_:
        await round_.wait_for_shots(4)

        putts = [
            stored
            for stored in await round_.history.shots()
            if stored.shot.club is not None and stored.shot.club.kind is ClubKind.PUTTER
        ]

        assert putts
        assert putts[0].shot.ball is not None
        assert putts[0].shot.ball.carry_yards is None


async def test_a_round_survives_the_process_being_restarted(
    tmp_path: Path,
) -> None:
    # The point of persistence: the file is still there afterwards, and it says
    # the session finished.
    path = tmp_path / "history.sqlite3"
    async with Round(database=path) as round_:
        await round_.wait_for_shots(2)
        session_id = round_.history.session_id

    reader = build_history_service(enabled=True, path=path)
    await reader.open()
    try:
        sessions = {session.session_id: session for session in await reader.sessions()}
        finished = sessions[session_id]
        assert finished.ended_at is not None
        assert finished.shot_count >= 2

        summary = await reader.analytics(session_id)
        assert summary is not None
        assert summary.shots >= 2
    finally:
        await reader.aclose()


async def test_raw_capture_keeps_the_bytes_the_device_sent(tmp_path: Path) -> None:
    async with Round(
        database=tmp_path / "history.sqlite3", record_raw_payloads=True
    ) as round_:
        await round_.wait_for_shots(1)

        first = (await round_.history.shots())[0]
        payload = await round_.history.payload(first.shot.shot_id)

        assert payload is not None
        assert '"BallData"' in payload.payload
        assert payload.source_kind == "gspro_connect"


async def test_a_round_with_history_switched_off_writes_no_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    async with Round() as round_:
        await round_.wait_until(lambda s: s.metrics is not None)

        assert round_.history.is_available is False

    assert not path.exists()
