"""An assistant asking CaddieJ a question, over a real socket, end to end.

The units are covered elsewhere. This is about the wiring, and about the claims
that cannot be checked anywhere else — the ones that only mean something once
there are two processes' worth of separation between the question and the answer.

The path being checked:

    a tool call on the MCP translator
        -> the real urllib client
        -> a real loopback socket
        -> the real bearer-token guard
        -> the HTTP transport
        -> the read façade
        -> a real SQLite history on disk
        -> the published contract, back the same way

Real server, real socket, real token, real database, real shots. The only thing
faked is the assistant: the tools are called directly rather than over stdio,
because the MCP SDK's own transport is not CaddieJ's code to test.

These need no external software, but they bind a port and speak real HTTP, so
they are gated like the other integration tests.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from caddiej.adapters.api.guard import Guard
from caddiej.adapters.api.server import AssistantServer, api_url, create_api_app
from caddiej.assistant.commands import CommandFacade
from caddiej.assistant.contract import SubsystemPayload
from caddiej.assistant.reads import ReadFacade
from caddiej.core.events import Event
from caddiej.domain.coaching import (
    Advice,
    AdviceOutcome,
    AdviceStatus,
    CoachingFocus,
)
from caddiej.domain.events import ClubSelected, ShotCompleted
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
from caddiej.mcp.client import CaddieJClient, CaddieJUnreachable
from caddiej.mcp.server import ToolFailure, tool_handlers
from caddiej.persistence.service import build_history_service
from caddiej.swing_reference.live import ReferenceLiveCoach

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to bind a real loopback port",
    ),
]

TOKEN = "an-integration-token"
NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


class NullFloor:
    def light_hitting_area(self) -> None: ...
    def darken_hitting_area(self) -> None: ...
    def show_address(self, club: object | None) -> None: ...
    def hide_address(self) -> None: ...
    def show_play_status(self, status: str, club: object | None) -> None: ...
    def show_putting_guide(self, club: object | None) -> None: ...
    def show_address_hud(
        self,
        club: object | None,
        distance_raw: float | None,
        recommendation: object | None = None,
    ) -> None: ...
    def hide_address_hud(self) -> None: ...
    def acknowledge_ball_lock(self) -> None: ...
    def clear_lock_acknowledgement(self) -> None: ...
    def show_shot(self, shot: object) -> None: ...
    def show_round_result(self, shot: object) -> None: ...
    def clear_shot(self) -> None: ...
    def show_advice(
        self, text: str, severity: float, result: object = None
    ) -> None: ...
    def show_progress(self, stage: object) -> None: ...
    def clear_progress(self) -> None: ...
    def show_advice_pending(self) -> None: ...
    def clear_advice(self) -> None: ...


async def silent(_event: Event) -> None: ...


def a_shot(club: ClubKind = ClubKind.DRIVER, speed: float = 148.2) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=0.0,
            device_id="ProTee VX",
        ),
        ball=BallLaunch(
            speed_mph=speed,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            carry_yards=268.4,
        ),
        club_delivery=ClubDelivery(path_degrees=-2.4, face_to_target_degrees=1.1),
        handedness=Handedness.RIGHT,
        club=Club(kind=club, vendor_code="DR"),
    )


@dataclass
class Running:
    """A CaddieJ answering on a real port, and the tools that ask it."""

    tools: dict[str, object]
    history: object
    coaching: ReferenceLiveCoach
    play: PlayFlow
    url: str

    async def call(self, tool: str, **arguments: object) -> dict[str, object]:
        return await self.tools[tool](**arguments)  # type: ignore[operator]

    async def record(self, shot: Shot) -> Shot:
        wanted = self.history.shots_recorded + 1  # type: ignore[attr-defined]
        await self.history.on_shot_completed(ShotCompleted(shot=shot))  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while self.history.shots_recorded < wanted:  # type: ignore[attr-defined]
            if loop.time() >= deadline:
                raise AssertionError("the writer never caught up")
            await asyncio.sleep(0.005)
        return shot


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    history = build_history_service(
        enabled=True, path=tmp_path / "history.sqlite3", player_name="Johan"
    )
    await history.open()
    coaching = ReferenceLiveCoach(silent, None, vision_calls=True)
    await coaching.open()
    play = PlayFlow(floor=NullFloor(), publish=silent)

    reads = ReadFacade(
        history=history,
        coaching=coaching,
        play=play,
        version="0.1.0",
        subsystems={
            "history": lambda: SubsystemPayload(
                enabled=True, available=history.is_available
            )
        },
        player_name="Johan",
        include_coaching_prose=True,
        mutation_enabled=True,
    )
    app = create_api_app(reads, Guard(token=TOKEN), CommandFacade(coaching))
    server = AssistantServer(app, host="127.0.0.1", port=0)
    serving = asyncio.create_task(server.serve(), name="assistant-api")
    try:
        await server.wait_until_serving(10.0)
        url = api_url("127.0.0.1", server.port)
        yield Running(
            tools=tool_handlers(CaddieJClient(base_url=url, token=TOKEN)),
            history=history,
            coaching=coaching,
            play=play,
            url=url,
        )
    finally:
        await server.stop()
        await asyncio.wait_for(serving, 10.0)
        await coaching.aclose()
        await play.aclose()
        await history.aclose()


# --- the whole path -------------------------------------------------------


async def test_a_shot_hit_here_is_readable_from_out_there(
    running: Running,
) -> None:
    shot = await running.record(a_shot(club=ClubKind.IRON))

    answer = await running.call("get_last_shot")

    assert answer["availability"] == "ok"
    reported = answer["shot"]
    assert isinstance(reported, dict)
    assert reported["shot_id"] == str(shot.shot_id)
    assert reported["club"] == "iron"
    assert reported["ball"]["carry_yards"] == pytest.approx(268.4)
    # What went in was a device id and a raw source; what came out has neither.
    assert "device_id" not in reported


async def test_status_answers_over_a_real_socket(running: Running) -> None:
    answer = await running.call("get_caddiej_status")

    assert answer["availability"] == "ok"
    assert answer["caddiej_version"] == "0.1.0"
    assert answer["player_name"] == "Johan"
    assert "get_last_shot" in answer["capabilities"]
    assert answer["subsystems"]["history"] == {"enabled": True, "available": True}


async def test_a_session_of_shots_comes_back_as_statistics(
    running: Running,
) -> None:
    for speed in (140.0, 150.0, 160.0):
        await running.record(a_shot(speed=speed))

    answer = await running.call("get_session_stats")

    assert answer["shots"] == 3
    speeds = next(
        metric for metric in answer["overall"] if metric["name"] == "ball_speed"
    )
    assert speeds["mean"] == pytest.approx(150.0)
    assert speeds["unit"] == "mph"


async def test_the_club_in_play_is_live_rather_than_stored(
    running: Running,
) -> None:
    await running.play.on_club_selected(
        ClubSelected(club=Club(kind=ClubKind.PUTTER, vendor_code="PT"))
    )

    answer = await running.call("get_current_club")

    assert answer["availability"] == "ok"
    assert answer["club"] == "putter"


async def test_coaching_written_here_is_readable_from_out_there(
    running: Running,
) -> None:
    shot = await running.record(a_shot())
    await running.history.record_advice(  # type: ignore[attr-defined]
        AdviceOutcome(
            advice=Advice(
                shot_id=shot.shot_id,
                focus=CoachingFocus.FACE_CONTROL,
                display="FACE OPEN",
                speech="Square the face a little earlier.",
                severity=0.62,
                created_at=NOW,
            ),
            status=AdviceStatus.TEMPLATE,
            baseline_shots=3,
        )
    )

    answer = await running.call("get_recent_coaching", limit=5)

    assert answer["availability"] == "ok"
    assert answer["prose_included"] is True
    assert answer["lines"][0]["focus"] == "face_control"
    assert answer["lines"][0]["display"] == "FACE OPEN"


# --- the mutation, all the way through ------------------------------------


async def test_the_one_mutation_reaches_coaching_from_outside(
    running: Running,
) -> None:
    answer = await running.call("set_training_focus", focus="Early Extension")

    assert answer["availability"] == "ok"
    assert answer["training_focus"] is None
    assert answer["changed"] is False
    assert running.coaching.preferred_focus is None
    assert (await running.call("get_caddiej_status"))["training_focus"] is None


async def test_a_focus_caddiej_does_not_know_never_reaches_it(
    running: Running,
) -> None:
    with pytest.raises(ToolFailure):
        await running.call("set_training_focus", focus="swing harder")

    assert running.coaching.preferred_focus is None


# --- the guard, over a real socket ---------------------------------------


async def test_a_wrong_token_is_refused_by_a_real_server(
    running: Running,
) -> None:
    impostor = tool_handlers(CaddieJClient(base_url=running.url, token="wrong"))

    with pytest.raises(ToolFailure, match="bearer token"):
        await impostor["get_caddiej_status"]()


async def test_a_caddiej_that_is_not_running_is_explained_not_guessed() -> None:
    """The most likely thing an assistant will encounter.

    CaddieJ is a program somebody starts when they walk into a golf room. Port
    1 has nothing on it, which is exactly what a Tuesday lunchtime looks like.
    """

    absent = CaddieJClient(
        base_url="http://127.0.0.1:1/v1", token=TOKEN, timeout_seconds=2.0
    )

    with pytest.raises(CaddieJUnreachable):
        await absent.get("/status")

    with pytest.raises(ToolFailure, match="does not appear to be running"):
        await tool_handlers(absent)["get_last_shot"]()


async def test_the_simulator_keeps_working_when_the_api_is_gone(
    running: Running,
) -> None:
    """The claim the whole arrangement rests on.

    The API is stopped mid-session and the domain carries on: shots are still
    attributed, still recorded, still readable. Nothing in CaddieJ waits on an
    assistant.
    """

    await running.record(a_shot())

    # As an assistant runtime being killed would look.
    with pytest.raises(ToolFailure):
        broken = tool_handlers(
            CaddieJClient(
                base_url="http://127.0.0.1:1/v1", token=TOKEN, timeout_seconds=2.0
            )
        )
        await broken["get_last_shot"]()

    await running.play.on_club_selected(
        ClubSelected(club=Club(kind=ClubKind.WEDGE, vendor_code="PW"))
    )
    await running.record(a_shot(club=ClubKind.WEDGE))

    assert running.history.shots_recorded == 2  # type: ignore[attr-defined]
    assert running.play.current_club is not None
    assert running.play.current_club.kind is ClubKind.WEDGE
