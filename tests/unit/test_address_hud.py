"""Address HUD: live club + DistanceToTarget while the ball is present."""

from __future__ import annotations

import asyncio

import pytest
from tests.unit.test_play_flow import Clock, FloorSpy, GatedClock, flow

from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    RoundShotObserved,
    ShotDispatched,
    TargetDistanceObserved,
)
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import PhysicalPoint
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService

pytestmark = pytest.mark.asyncio

DRIVER = Club(kind=ClubKind.DRIVER, vendor_code="Driver")
SEVEN_IRON = Club(kind=ClubKind.IRON, vendor_code="7 Iron")
IRON = Club(kind=ClubKind.IRON, vendor_code="Iron")


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(
        shot_id=shot_id,
        ball_speed_raw=32.8386,
        total_distance_raw=28.0288715,
        distance_to_pin_raw=289.443268,
    )


async def _publish(_event: object) -> None:
    return None


def presenter_stack() -> tuple[FloorService, PlayFlow]:
    floor = FloorService()
    presenter = FloorPresenter(
        floor,
        layout_from_tees(
            tee=PhysicalPoint(x_m=0.5, y_m=0.72),
            driver_tee=PhysicalPoint(x_m=0.35, y_m=0.50),
        ),
    )
    subject = PlayFlow(floor=presenter, publish=_publish, sleep=Clock().sleep)
    return floor, subject


async def test_ready_shows_the_target_before_a_ball_has_been_detected() -> None:
    """READY is when the player is choosing a club, so it is when the distance
    is worth having. Waiting for the ball meant the number arrived as the player
    was already standing over it."""

    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))

    # Two independent layers: where the ball goes, and how far it is to the pin.
    assert floor.showing_address is True
    assert floor.showing_address_hud is True
    assert floor.address_distance == 347.0

    await subject.on_ball_detected(BallDetected())

    assert floor.showing_address is False
    assert floor.showing_address_hud is True
    assert floor.address_club is DRIVER
    assert floor.address_distance == 347.0
    await subject.aclose()


async def test_finding_ball_keeps_the_target_on_the_floor() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    subject.start()
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    assert floor.play_status == "FINDING BALL"
    assert floor.showing_address_hud is True
    assert floor.address_distance == 147.2
    await subject.aclose()


async def test_club_changes_while_the_ball_is_present() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
    await subject.on_ball_detected(BallDetected())

    await subject.on_club_selected(ClubSelected(club=SEVEN_IRON))

    assert floor.showing_address_hud is True
    assert floor.address_club is SEVEN_IRON
    assert floor.address_distance == 147.2
    await subject.aclose()


async def test_shot_hides_address_hud_and_shows_the_result() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))
    await subject.on_ball_detected(BallDetected())
    await subject.on_shot_dispatched(ShotDispatched())

    assert floor.showing_address_hud is False

    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    assert floor.round_results[-1].shot_id == "1"
    assert floor.showing_address_hud is False
    await subject.aclose()


async def test_ready_after_a_shot_does_not_keep_the_address_hud() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))
    await subject.on_ball_detected(BallDetected())
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.showing_address_hud is False
    assert floor.showing_address is True
    await subject.aclose()


async def test_next_ball_uses_the_new_target_distance_not_the_previous_one() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))
    await subject.on_ball_detected(BallDetected())
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    await subject.on_target_distance(TargetDistanceObserved(distance_raw=289.4))
    await subject.on_club_selected(ClubSelected(club=IRON))
    await subject.on_ball_detected(BallDetected())
    # The result panel and the pre-shot numbers are the same size in the same
    # part of the surface, so the result owns the floor until it has been read.
    assert floor.showing_address_hud is False
    await clock.elapse_duration(8.0)

    assert floor.showing_address_hud is True
    assert floor.address_club is IRON
    assert floor.address_distance == 289.4
    await subject.aclose()


async def test_removing_the_ball_returns_the_marker_without_taking_the_target() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))
    await subject.on_ball_detected(BallDetected())

    await subject.on_ball_cleared(BallCleared())

    assert floor.showing_address is True
    assert floor.showing_address_hud is True
    assert floor.address_distance == 347.0
    await subject.aclose()


async def test_presenter_formats_live_distance_and_vendor_club_name() -> None:
    floor, subject = presenter_stack()
    subject.start()
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=SEVEN_IRON))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
    await subject.on_ball_detected(BallDetected())

    assert floor.state.marker.visible is False
    assert floor.state.address is not None
    assert floor.state.address.distance_display == "147 m"
    assert floor.state.address.club_display == "7 IRON"
    await subject.aclose()


async def test_presenter_does_not_show_previous_shot_travel_as_address_distance() -> (
    None
):
    floor, subject = presenter_stack()
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=DRIVER))
    await subject.on_target_distance(TargetDistanceObserved(distance_raw=347.0))
    await subject.on_ball_detected(BallDetected())
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)

    assert floor.state.address is None
    assert floor.state.round_result is not None
    assert floor.state.round_result.shot_distance_display == "28 m"
    # Same source as the address panel's live number, at a different moment: this
    # is what was left after the shot, not what was left before it.
    assert floor.state.round_result.remaining_distance_display == "289 m"
    await subject.aclose()
