"""PlayFlow through the real floor presenter: marker geometry and club labels."""

import asyncio

import pytest

from caddiej.domain.events import (
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    RoundShotObserved,
)
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import PhysicalPoint
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService

pytestmark = pytest.mark.asyncio

TEE = PhysicalPoint(x_m=0.5, y_m=0.72)
DRIVER_TEE = PhysicalPoint(x_m=0.35, y_m=0.50)


class Clock:
    def __init__(self) -> None:
        self._gates: list[asyncio.Event] = []

    async def sleep(self, _seconds: float) -> None:
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()


async def _publish(_event: object) -> None:
    return None


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(
        shot_id=shot_id, ball_speed_raw=32.8386, total_distance_raw=28.0288715
    )


def stack() -> tuple[FloorService, PlayFlow]:
    floor = FloorService()
    presenter = FloorPresenter(
        floor, layout_from_tees(tee=TEE, driver_tee=DRIVER_TEE), club_path_length=0.4
    )
    subject = PlayFlow(
        floor=presenter,
        publish=_publish,
        shot_result_min_duration=8.0,
        shot_result_max_duration=30.0,
        sleep=Clock().sleep,
    )
    return floor, subject


async def test_repeated_shots_bring_the_marker_back_each_time() -> None:
    floor, subject = stack()
    subject.start()
    assert floor.state.marker.visible is True
    assert floor.state.hitting_strip is not None
    assert floor.state.hitting_strip.visible is True

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_ball_detected(BallDetected())
    assert floor.state.marker.visible is False
    # The marker steps aside for the ball; the light over the mat does not. This
    # used to be the other way round on real hardware, which put the bay in the
    # dark at exactly the moment somebody was standing in it.
    assert floor.state.hitting_strip is not None
    assert floor.state.hitting_strip.visible is True

    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    # The result is still readable at the top of the surface; the marker comes
    # back underneath it, which is what the next ball needs.
    assert floor.state.round_result is not None
    assert floor.state.marker.visible is True

    # Shots two through four, as the room actually produces them. The regression
    # this covers was a marker that came back for the first shot of a session
    # and never again, because readiness — not ball placement — was deciding it.
    for shot in range(2, 5):
        await subject.on_ball_detected(BallDetected())
        assert floor.state.marker.visible is False, (
            f"marker under the ball, shot {shot}"
        )

        await subject.on_round_shot(RoundShotObserved(shot=a_round(str(shot))))
        await asyncio.sleep(0)
        # ProTee goes to FINDING BALL between shots, which reads as not ready.
        # That is when the player most needs to be shown where to put one.
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))
        assert floor.state.marker.visible is True, f"marker missing after shot {shot}"

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        assert floor.state.marker.visible is True, f"marker missing after shot {shot}"
    await subject.aclose()


async def test_iron_then_driver_then_iron_moves_the_marker() -> None:
    floor, subject = stack()
    subject.start()

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    assert floor.state.club_label == "IRON"
    assert floor.state.marker.position.x == pytest.approx(TEE.x_m)
    assert floor.state.marker.position.y == pytest.approx(TEE.y_m)

    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    assert floor.state.club_label == "DRIVER"
    assert floor.state.marker.visible is True
    assert floor.state.marker.position.x == pytest.approx(DRIVER_TEE.x_m)
    assert floor.state.marker.position.y == pytest.approx(DRIVER_TEE.y_m)

    await subject.on_round_shot(RoundShotObserved(shot=a_round("2")))
    await asyncio.sleep(0)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    assert floor.state.club_label == "IRON"
    assert floor.state.marker.position.x == pytest.approx(TEE.x_m)
    assert floor.state.marker.position.y == pytest.approx(TEE.y_m)
    await subject.aclose()


async def test_unknown_club_uses_the_default_tee() -> None:
    floor, subject = stack()
    subject.start()
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.state.club_label == ""
    assert floor.state.marker.position.x == pytest.approx(TEE.x_m)
    assert floor.state.marker.position.y == pytest.approx(TEE.y_m)
    await subject.aclose()
