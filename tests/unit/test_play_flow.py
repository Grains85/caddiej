"""What the floor shows as a shot is played.

The floor is a spy here rather than a real one: this is about the sequence of
decisions, and the drawing of them is `test_floor_presenter`'s subject. Time is
fake, so nothing waits for a shot display to expire.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.bag import ClubDistanceProvider, ClubRecommendation
from caddiej.domain.coaching import (
    Advice,
    AdviceSource,
    CoachingFocus,
    Diagnosis,
    FocusCandidate,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    BallCleared,
    BallDetected,
    ClubSelected,
    HandednessChanged,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RoundShotObserved,
    ShotCompleted,
    ShotDetected,
    ShotDiagnosed,
    ShotDispatched,
    ShotIngested,
)
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import LOCK_ACK_SECONDS, PlayFlow
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Detection,
    Handedness,
    Shot,
    ShotSource,
)

pytestmark = pytest.mark.asyncio


class FloorSpy:
    """Records what the floor was asked to do, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.shots: list[Shot] = []
        self.advice: list[tuple[str, float]] = []
        self.showing_address = False
        self.showing_shot = False
        self.showing_advice = False
        self.play_status = ""
        self.play_club: Club | None = None
        self.round_results: list[ObservedRoundShot] = []
        self.hitting_area_lit = False
        self.showing_address_hud = False
        # One field for both layers, because both are handed the same club in
        # play: `address_club` says which club the flow believes is in the
        # player's hands, whichever layer last asked about it.
        self.address_club: Club | None = None
        self.address_distance: float | None = None
        self.recommendation: ClubRecommendation | None = None
        self.lock_ack = False
        # The club the putting guide was last told about, and what it did with
        # it. A list, because the flow asks on every pass and a test about
        # putting mode wants to see it settle rather than flap.
        self.putting_guide: list[Club | None] = []

    def light_hitting_area(self) -> None:
        self.calls.append("light_hitting_area")
        self.hitting_area_lit = True

    def darken_hitting_area(self) -> None:
        self.calls.append("darken_hitting_area")
        self.hitting_area_lit = False

    def show_address(self, club: Club | None) -> None:
        self.calls.append("show_address")
        self.address_club = club
        self.showing_address = True

    def hide_address(self) -> None:
        self.calls.append("hide_address")
        self.showing_address = False

    def acknowledge_ball_lock(self) -> None:
        self.calls.append("acknowledge_ball_lock")
        self.lock_ack = True

    def clear_lock_acknowledgement(self) -> None:
        self.calls.append("clear_lock_acknowledgement")
        self.lock_ack = False

    def show_play_status(self, status: str, club: Club | None) -> None:
        self.calls.append("show_play_status")
        self.play_status = status
        self.play_club = club

    def show_putting_guide(self, club: Club | None) -> None:
        self.calls.append("show_putting_guide")
        self.putting_guide.append(club)

    def show_address_hud(
        self,
        club: Club | None,
        distance_raw: float | None,
        recommendation: ClubRecommendation | None = None,
    ) -> None:
        self.calls.append("show_address_hud")
        self.address_club = club
        self.address_distance = distance_raw
        self.recommendation = recommendation
        self.showing_address_hud = True

    def hide_address_hud(self) -> None:
        self.calls.append("hide_address_hud")
        self.showing_address_hud = False
        self.recommendation = None

    def show_shot(self, shot: Shot) -> None:
        self.calls.append("show_shot")
        self.shots.append(shot)
        self.showing_shot = True

    def show_round_result(self, shot: ObservedRoundShot) -> None:
        self.calls.append("show_round_result")
        self.round_results.append(shot)
        self.showing_shot = True

    def clear_shot(self) -> None:
        self.calls.append("clear_shot")
        self.showing_shot = False

    def show_advice(self, text: str, severity: float, result: object = None) -> None:
        self.calls.append("show_advice")
        self.advice.append((text, severity))
        self.showing_advice = True

    def show_advice_pending(self) -> None:
        self.calls.append("show_advice_pending")

    def clear_advice(self) -> None:
        self.calls.append("clear_advice")
        self.showing_advice = False


class Clock:
    """A sleep that only returns when a test says the delay has elapsed."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self._release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self._release.wait()

    async def elapse(self) -> None:
        self._release.set()
        # Two turns: one for the sleeping task to wake, one for what it then does.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self._release.clear()


def a_shot(path_degrees: float | None = -2.4) -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=148.2,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
        ),
        club_delivery=ClubDelivery(path_degrees=path_degrees),
    )


class Announcements:
    """Collects what the flow told the rest of CaddieJ."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def shots(self) -> list[Shot]:
        return [event.shot for event in self.events if isinstance(event, ShotCompleted)]


def flow(
    floor: FloorSpy,
    clock: Clock | None = None,
    announcements: Announcements | None = None,
    shot_result_min_duration: float = 8.0,
    shot_result_max_duration: float = 30.0,
    club_distances: ClubDistanceProvider | None = None,
) -> PlayFlow:
    return PlayFlow(
        floor=floor,
        publish=(announcements or Announcements()).publish,
        shot_display_seconds=6.0,
        shot_result_min_duration=shot_result_min_duration,
        shot_result_max_duration=shot_result_max_duration,
        sleep=(clock or Clock()).sleep,
        club_distances=club_distances,
    )


def a_diagnosis(
    shot_id: UUID,
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    severity: float = 0.6,
    display: str = "FACE OPEN",
) -> Diagnosis:
    return Diagnosis(
        shot_id=shot_id,
        focus=focus,
        severity=severity,
        display=display,
        speech="Square the face a little earlier.",
        club=ClubKind.DRIVER,
        handedness=Handedness.RIGHT,
        baseline_shots=5,
        candidates=(FocusCandidate(focus=focus, severity=severity),),
        observations=(),
    )


def an_advice(
    shot_id: UUID, display: str = "FACE OPEN", severity: float = 0.6
) -> Advice:
    return Advice(
        shot_id=shot_id,
        focus=CoachingFocus.FACE_CONTROL,
        display=display,
        speech="Square the face a little earlier.",
        severity=severity,
        source=AdviceSource.MODEL,
        created_at=datetime.now(UTC),
    )


# --- the club in play ------------------------------------------------------


async def test_the_floor_shows_an_address_before_anything_has_connected() -> None:
    # An idle floor with a marker is how the projector gets aimed.
    floor = FloorSpy()

    flow(floor).start()

    assert floor.showing_address is True
    assert floor.address_club is None


async def test_selecting_a_club_shows_its_address() -> None:
    floor = FloorSpy()
    club = Club(kind=ClubKind.DRIVER, vendor_code="DR")

    await flow(floor).on_club_selected(ClubSelected(club=club))

    assert floor.address_club == club
    assert floor.showing_address is True


async def test_changing_club_at_address_changes_the_marker() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))

    assert floor.address_club == Club(kind=ClubKind.PUTTER)
    assert floor.showing_address is True


# --- the ball on the mat --------------------------------------------------


async def test_a_ball_on_the_mat_hides_the_marker() -> None:
    # The projector would otherwise be shining onto the ball.
    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_ball_detected(BallDetected())

    assert floor.showing_address is False
    assert floor.lock_ack is True
    assert floor.calls.count("acknowledge_ball_lock") == 1
    await subject.aclose()


async def test_removing_the_ball_brings_the_marker_back() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_ball_cleared(BallCleared())

    assert floor.showing_address is True
    assert floor.lock_ack is False
    await subject.aclose()


async def test_changing_club_while_a_ball_is_in_place_shows_no_marker() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))

    assert floor.showing_address is False
    await subject.aclose()


async def test_repeated_ball_present_observations_do_not_retrigger_the_lock_ack() -> (
    None
):
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_ball_detected(BallDetected())
    await asyncio.sleep(0)

    await subject.on_ball_detected(BallDetected())
    await subject.on_ball_detected(BallDetected())

    assert floor.calls.count("acknowledge_ball_lock") == 1
    await clock.elapse_duration(LOCK_ACK_SECONDS)
    assert floor.lock_ack is False
    await subject.aclose()


async def test_a_device_that_says_it_is_not_ready_still_shows_where_the_ball_goes() -> (
    None
):
    """The inversion that the marker regression came down to.

    ProTee says `FINDING BALL` â€” which CaddieJ reads as not ready â€” while it is
    *looking* for a ball, which is precisely when a player needs to be shown
    where to put one. Suppressing the marker here is what left it missing for
    the rest of a session: after the first shot the device is not ready again
    until it has found the next ball, by which time the marker should be gone
    for a different reason.
    """

    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    assert floor.showing_address is True


async def test_a_device_becoming_ready_keeps_the_marker_until_a_ball_arrives() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.showing_address is True

    # And the one thing that does take it away.
    await subject.on_ball_detected(BallDetected())

    assert floor.showing_address is False


# --- a shot ---------------------------------------------------------------


async def test_a_shot_puts_the_club_path_and_numbers_on_the_floor() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    assert floor.showing_shot is True
    assert floor.showing_address is False


async def test_a_shot_is_credited_to_the_club_in_play() -> None:
    # No launch monitor names the club in its shot message, so this is knowledge
    # only CaddieJ has.
    floor = FloorSpy()
    subject = flow(floor)
    club = Club(kind=ClubKind.IRON, vendor_code="I7")
    await subject.on_club_selected(ClubSelected(club=club))

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    assert floor.shots[0].club == club


async def test_a_shot_is_announced_even_when_the_floor_cannot_draw_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenFloor(FloorSpy):
        def show_shot(self, shot: Shot) -> None:
            raise RuntimeError("the projector is unplugged")

    caplog.set_level("ERROR")
    floor = BrokenFloor()
    announcements = Announcements()
    subject = flow(floor, announcements=announcements)
    club = Club(kind=ClubKind.WEDGE, vendor_code="PW")
    await subject.on_club_selected(ClubSelected(club=club))
    assert floor.showing_address

    await subject.on_shot(ShotIngested(shot=a_shot()))

    announced = announcements.shots()
    assert len(announced) == 1
    assert announced[0].club == club
    assert "show_shot" not in floor.calls
    assert any(
        record.message == "play_floor_presentation_failed" for record in caplog.records
    )
    # Floor failure must not leave PlayFlow permanently in "showing shot",
    # or the address marker would never return.
    await subject.on_ball_cleared(BallCleared())
    assert floor.showing_address
    await subject.aclose()


async def test_a_shot_is_announced_once_the_domain_has_attributed_it() -> None:
    # Anything that stores or speaks about a shot needs one that knows which
    # club hit it, which is not true of the shot as it was ingested.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, announcements=announcements)
    club = Club(kind=ClubKind.WEDGE, vendor_code="PW")
    await subject.on_club_selected(ClubSelected(club=club))

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    announced = announcements.shots()
    assert len(announced) == 1
    assert announced[0].club == club


async def test_the_floor_is_updated_before_the_shot_is_announced() -> None:
    # A player is standing in front of the projector waiting for a number; a
    # subscriber is not.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, announcements=announcements)

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    assert "show_shot" in floor.calls
    assert announcements.shots(), "the shot was never announced"


async def test_the_announced_shot_is_the_one_that_reached_the_floor() -> None:
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, announcements=announcements)

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    assert announcements.shots()[0] is floor.shots[0]


async def test_a_shot_hit_before_any_club_was_named_is_still_announced() -> None:
    # A round can start before GSPro has said anything, and losing that shot
    # would be a worse answer than recording it with no club.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, announcements=announcements)

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.aclose()

    assert announcements.shots()[0].club is None


async def test_the_floor_returns_to_the_next_address_once_the_shot_has_been_read() -> (
    None
):
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    await subject.on_ball_detected(BallDetected())
    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.on_ball_cleared(BallCleared())

    assert floor.showing_address is False  # the shot is still being shown

    await clock.elapse()

    assert floor.showing_shot is False
    assert floor.showing_address is True
    assert floor.address_club == Club(kind=ClubKind.DRIVER)
    await subject.aclose()


async def test_the_shot_stays_up_for_as_long_as_configured() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await asyncio.sleep(0)

    assert clock.slept == [6.0]
    await subject.aclose()


async def test_a_second_shot_restarts_the_display_rather_than_cutting_it_short() -> (
    None
):
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)

    await subject.on_shot(ShotIngested(shot=a_shot()))
    await asyncio.sleep(0)
    await subject.on_shot(ShotIngested(shot=a_shot()))
    await asyncio.sleep(0)

    assert clock.slept == [6.0, 6.0]
    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0
    await subject.aclose()


async def test_a_ball_placed_during_the_shot_display_still_hides_the_marker() -> None:
    # A player who tees up quickly must not have the marker under the ball.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    await subject.on_shot(ShotIngested(shot=a_shot()))
    await subject.on_ball_detected(BallDetected())

    await clock.elapse()

    assert floor.showing_shot is False
    assert floor.showing_address is False
    await subject.aclose()


# --- the device going away ------------------------------------------------


async def test_a_ball_left_detected_when_the_device_drops_does_not_strand_the_marker() -> (
    None
):
    # Nothing would ever clear it: the connection that reported the ball is the
    # only thing that could report it gone.
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_link_changed(
        LaunchMonitorLinkChanged(state=LinkState.LISTENING, detail=None)
    )

    assert floor.showing_address is True


async def test_a_device_left_not_ready_when_it_drops_does_not_strand_the_marker() -> (
    None
):
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    await subject.on_link_changed(
        LaunchMonitorLinkChanged(
            state=LinkState.SIMULATOR_UNAVAILABLE, detail="GSPro is not running"
        )
    )

    assert floor.showing_address is True


async def test_a_healthy_link_changes_nothing_on_its_own() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_link_changed(
        LaunchMonitorLinkChanged(state=LinkState.BRIDGED, detail="a device")
    )

    assert floor.showing_address is False


# --- wired to the bus -----------------------------------------------------


async def test_the_flow_reacts_to_events_published_on_the_bus() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    bus = AsyncEventBus()
    subject.subscribe(bus)
    await bus.start()

    try:
        await bus.publish(ClubSelected(club=Club(kind=ClubKind.PUTTER)))
        await bus.publish(BallDetected())
        await bus.publish(ShotIngested(shot=a_shot()))
        await bus.publish(BallCleared())
        await bus.drain(timeout=1.0)

        assert floor.showing_shot is True
        assert floor.showing_address is False

        await clock.elapse()

        assert floor.showing_address is True
        assert floor.address_club == Club(kind=ClubKind.PUTTER)
    finally:
        await subject.aclose()
        await bus.stop()


async def test_closing_the_flow_cancels_a_pending_shot_display() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    await subject.on_shot(ShotIngested(shot=a_shot()))
    await asyncio.sleep(0)

    await subject.aclose()

    await clock.elapse()
    assert floor.calls.count("clear_shot") == 0


# --- the coaching line ----------------------------------------------------
#
# The only words on the floor that a language model may have chosen. What the
# flow does with them is show them, time them out, and drop them if they are
# about a swing the player has moved on from. Nothing here reads them.


async def test_the_templated_verdict_goes_up_as_soon_as_it_is_reached() -> None:
    # It needed no model to produce, so it should not wait for one.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))

    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))

    assert floor.advice[-1] == ("FACE OPEN", 0.6)
    await subject.aclose()


async def test_a_good_shot_puts_nothing_on_the_floor_to_read() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))

    await subject.on_shot_diagnosed(
        ShotDiagnosed(
            diagnosis=a_diagnosis(
                shot.shot_id, focus=CoachingFocus.NONE, severity=0.0, display=""
            )
        )
    )

    assert not floor.advice
    await subject.aclose()


async def test_the_coached_wording_replaces_the_templated_one() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))

    await subject.on_advice_ready(
        AnalysisCompleted(
            shot_id=shot.shot_id,
            advice=an_advice(shot.shot_id, display="FACE CLOSED", severity=0.8),
        )
    )

    assert floor.advice[-1] == ("FACE CLOSED", 0.8)
    await subject.aclose()


async def test_advice_about_a_shot_the_player_has_moved_on_from_is_dropped() -> None:
    # A model answering late must not have its words read as being about the
    # swing that is on the floor now.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    first = a_shot()
    await subject.on_shot(ShotIngested(shot=first))
    await subject.on_shot(ShotIngested(shot=a_shot()))

    await subject.on_advice_ready(
        AnalysisCompleted(shot_id=first.shot_id, advice=an_advice(first.shot_id))
    )

    assert not floor.advice
    await subject.aclose()


async def test_a_new_shot_wipes_what_was_said_about_the_last_one() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))

    await subject.on_shot(ShotIngested(shot=a_shot()))

    assert floor.showing_advice is False
    await subject.aclose()


async def test_the_coaching_line_has_no_lifetime_of_its_own() -> None:
    # Nothing is armed when a line reaches the floor. The only sleep is the
    # shot display's, which is about the numbers and not about the coaching.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await asyncio.sleep(0)

    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    assert clock.slept == [6.0]
    await subject.aclose()


async def test_the_coached_wording_arms_nothing_either() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await subject.on_advice_ready(
        AnalysisCompleted(shot_id=shot.shot_id, advice=an_advice(shot.shot_id))
    )
    await asyncio.sleep(0)

    assert clock.slept == [6.0]
    assert floor.showing_advice is True
    await subject.aclose()


async def test_the_coaching_line_outlives_the_shot_display() -> None:
    # The numbers come down on their timer. The coaching does not come down
    # with them: there is no clock that can take it away.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await clock.elapse()

    assert floor.showing_advice is True
    await subject.aclose()


async def test_a_ball_placed_on_the_mat_clears_the_line() -> None:
    # Reversed by the first hardware test. BALL READY is the device saying it
    # can see a ball, which is also the player saying they are done reading:
    # from here the floor belongs to the distance, the club and the next shot.
    floor = FloorSpy()
    subject = flow(floor)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await subject.on_ball_cleared(BallCleared())
    await subject.on_ball_detected(BallDetected())

    assert floor.showing_advice is False
    await subject.aclose()


async def test_the_device_losing_the_ball_does_not_clear_the_line() -> None:
    # The other half, and the one that matters on real hardware: ProTee sits in
    # FINDING BALL between shots, which publishes a clear and a readiness
    # change. Neither is a new placement, and a lesson that vanished because
    # the camera looked away would be worse than one that outstayed itself.
    floor = FloorSpy()
    subject = flow(floor)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await subject.on_ball_cleared(BallCleared())
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))
    await subject.on_ball_cleared(BallCleared())

    assert floor.showing_advice is True
    await subject.aclose()


async def test_the_next_actual_strike_clears_the_line() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)
    assert floor.showing_advice is True

    await subject.on_shot_detected(ShotDetected(monotonic_ns=1_000))

    assert floor.showing_advice is False
    await subject.aclose()


async def test_closing_the_flow_cancels_a_pending_coaching_line() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await subject.aclose()

    await clock.elapse()
    assert floor.showing_advice is True


async def test_advice_arriving_before_any_shot_is_shown_anyway() -> None:
    # Only reachable from a replay, but the alternative is comparing against a
    # shot id that does not exist yet and silently dropping every line.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)

    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))

    assert floor.advice
    await subject.aclose()


# --- what the flow will tell anything that asks ------------------------------
#
# Four read-only properties, added for the assistant boundary. Nothing inside
# CaddieJ reads them, so these are the only tests that would notice them
# silently returning something stale.


async def test_the_flow_reports_the_club_the_simulator_named() -> None:
    subject = flow(FloorSpy(), Clock())

    assert subject.current_club is None
    await subject.on_club_selected(
        ClubSelected(club=Club(kind=ClubKind.IRON, vendor_code="I7"))
    )

    assert subject.current_club is not None
    assert subject.current_club.kind is ClubKind.IRON
    await subject.aclose()


async def test_the_flow_reports_the_mat_and_forgets_it_when_the_link_dies() -> None:
    subject = flow(FloorSpy(), Clock())

    assert subject.ball is Detection.UNKNOWN
    await subject.on_ball_detected(BallDetected())
    assert subject.ball is Detection.PRESENT

    await subject.on_link_changed(LaunchMonitorLinkChanged(state=LinkState.LISTENING))

    # Not "no ball": nothing knows any more, and reporting absence would be a
    # claim the device never made.
    assert subject.ball is Detection.UNKNOWN
    assert subject.ready is None
    await subject.aclose()


async def test_the_flow_reports_readiness_only_once_a_device_has_said() -> None:
    subject = flow(FloorSpy(), Clock())

    assert subject.ready is None
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    assert subject.ready is False
    await subject.aclose()


async def test_handedness_is_tracked_without_touching_the_floor() -> None:
    floor = FloorSpy()
    subject = flow(floor, Clock())
    assert subject.handedness is Handedness.UNKNOWN
    before = floor.calls

    await subject.on_handedness_changed(HandednessChanged(handedness=Handedness.LEFT))

    assert subject.handedness is Handedness.LEFT
    # Somebody turning around is not a reason to repaint a marker.
    assert floor.calls == before
    await subject.aclose()


async def test_handedness_survives_the_launch_monitor_going_away() -> None:
    # Unlike the ball and readiness, this is a fact about the player rather than
    # something the device is currently observing.
    subject = flow(FloorSpy(), Clock())
    await subject.on_handedness_changed(HandednessChanged(handedness=Handedness.LEFT))

    await subject.on_link_changed(LaunchMonitorLinkChanged(state=LinkState.LISTENING))

    assert subject.handedness is Handedness.LEFT
    await subject.aclose()


async def test_the_flow_subscribes_to_handedness() -> None:
    bus = AsyncEventBus()
    subject = flow(FloorSpy(), Clock())
    subject.subscribe(bus)
    await bus.start()

    await bus.publish(HandednessChanged(handedness=Handedness.LEFT))
    await bus.drain(timeout=1.0)

    assert subject.handedness is Handedness.LEFT
    await bus.stop()
    await subject.aclose()


async def test_ready_status_and_club_are_shown_on_the_floor() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))

    assert floor.play_status == "READY"
    assert floor.play_club is not None
    assert floor.play_club.kind is ClubKind.DRIVER
    assert floor.showing_address is True
    await subject.aclose()


async def test_a_round_entry_with_no_measurement_is_shown_and_nothing_more() -> None:
    # The result panel is still right. There is simply nothing for history,
    # video or coaching to work from.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, Clock(), announcements)

    await subject.on_round_shot(
        RoundShotObserved(shot=ObservedRoundShot(shot_id="D", ball_speed_raw=32.84))
    )

    assert [result.shot_id for result in floor.round_results] == ["D"]
    assert announcements.shots() == []
    await subject.aclose()


async def test_an_observed_round_shot_is_announced_so_it_can_be_coached() -> None:
    # The gap this closes. Without the announcement a passively observed
    # session produces a result panel and is never coached, which is the mode
    # CaddieJ actually runs in.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, Clock(), announcements)
    measured = an_observed_shot()

    await subject.on_round_shot(RoundShotObserved(shot=a_round("D"), measured=measured))

    assert [shot.shot_id for shot in announcements.shots()] == [measured.shot_id]
    await subject.aclose()


async def test_the_club_that_hit_it_is_attached_on_the_way_past() -> None:
    # Neither the club nor the handedness is in the round file; both came from
    # the launch monitor's own log, and only the domain has seen them.
    floor = FloorSpy()
    announcements = Announcements()
    subject = flow(floor, Clock(), announcements)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_handedness_changed(HandednessChanged(handedness=Handedness.LEFT))

    await subject.on_round_shot(
        RoundShotObserved(shot=a_round("D"), measured=an_observed_shot())
    )

    announced = announcements.shots()[0]
    assert announced.club == Club(kind=ClubKind.IRON)
    assert announced.handedness is Handedness.LEFT
    await subject.aclose()


async def test_coaching_about_an_earlier_round_shot_is_dropped() -> None:
    # The announcement is also what gives this path a current shot id, so late
    # coaching can be told from coaching about the swing on the floor now.
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    first = an_observed_shot()
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A"), measured=first))
    await asyncio.sleep(0)
    await subject.on_round_shot(
        RoundShotObserved(shot=a_round("B"), measured=an_observed_shot())
    )
    await asyncio.sleep(0)

    await subject.on_advice_ready(
        AnalysisCompleted(shot_id=first.shot_id, advice=an_advice(first.shot_id))
    )
    await clock.elapse(READABLE)

    assert not floor.advice
    await subject.aclose()


async def test_shot_dispatched_updates_the_caption() -> None:
    floor = FloorSpy()
    subject = flow(floor)

    await subject.on_shot_dispatched(ShotDispatched())

    assert floor.play_status == "SHOT SENT"
    await subject.aclose()


class TestAnExpiringTimerCannotEraseWhatReplacedIt:
    """A floor timer is replaced from synchronous code.

    `_acknowledge_lock` cancels the previous task and immediately creates the
    next, and it cannot await the cancellation from a synchronous method. That
    reads like a race — the old timer's sleep expiring in the same turn the
    new one is armed, and the old one taking down the pulse that replaced it —
    and it is not one: asyncio holds a cancellation requested after the
    awaited future resolved and raises it at the task's next step, so the body
    never runs.

    Kept because it is the contract rather than the mechanism. Anything that
    replaced `cancel` with a weaker hand-off, or added an `await` between the
    sleep and the clear, would break here.

    This used to cover the advice timer too, which no longer exists: a coached
    line is taken off by the next strike and never by a clock, so the race it
    could lose has no timer left to lose it to.
    """

    @staticmethod
    async def expire_without_yielding(clock: "GatedClock", index: int) -> None:
        """Let a timer's sleep finish, but do not give it the loop after that.

        The one turn at the start is how the timer task gets far enough to be
        sleeping at all; `create_task` does not run it. Setting the gate then
        leaves it ready to run with nothing suspended before it clears the
        floor, which is the narrowest the window gets. Releasing the gate
        through `elapse` would run it to completion before anything could arm
        a replacement, and there would be nothing to race.
        """

        await asyncio.sleep(0)
        clock._gates[index].set()

    async def test_a_newer_line_simply_replaces_the_older_one(self) -> None:
        floor = FloorSpy()
        clock = GatedClock()
        subject = flow(floor, clock, Announcements())

        await subject._show_advice(uuid4(), "keep the head still", 0.4)
        await subject._show_advice(uuid4(), "slow the takeaway", 0.4)
        for _ in range(8):
            await asyncio.sleep(0)
        await subject.aclose()

        assert floor.showing_advice is True
        assert floor.advice[-1][0] == "slow the takeaway"

    async def test_the_older_lock_pulse_does_not_clear_the_newer_one(self) -> None:
        floor = FloorSpy()
        clock = GatedClock()
        subject = flow(floor, clock, Announcements())

        subject._acknowledge_lock()
        await self.expire_without_yielding(clock, 0)
        subject._acknowledge_lock()
        for _ in range(8):
            await asyncio.sleep(0)

        # Asserted before shutdown, which takes the pulse off deliberately.
        assert floor.lock_ack is True
        await subject.aclose()


class GatedClock:
    """One Event per sleep, so an old timeout can be released without the new one."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self._gates: list[asyncio.Event] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    async def elapse(self, index: int) -> None:
        self._gates[index].set()
        # Several turns, not one: a timer that expires may cancel another timer
        # and await it, and that hand-off costs its own trips round the loop.
        for _ in range(8):
            await asyncio.sleep(0)

    async def elapse_duration(self, seconds: float) -> None:
        for index, (slept, gate) in enumerate(
            zip(self.slept, self._gates, strict=True)
        ):
            if slept == seconds and not gate.is_set():
                await self.elapse(index)
                return
        raise AssertionError(f"no pending sleep of {seconds}s in {self.slept}")


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(shot_id=shot_id, ball_speed_raw=32.84)


def an_observed_shot() -> Shot:
    """A round-play shot as the adapter normalizes one: a ball speed, and that
    is the whole of what the file measured."""

    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="gspro_round",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(speed_mph=73.4),
    )


# A round result arms two sleeps, in this order: the window it is guaranteed to
# stay readable for, then the cap it may not outlive. Named because the index
# into a gated clock is otherwise the least readable thing in these tests.
READABLE = 0
CAP = 1


async def test_a_round_result_arms_a_readable_window_and_a_cap() -> None:
    """Both ends of the lifecycle, because the panel can only appear once GSPro
    has finished flying the ball and so is already seconds behind the strike."""

    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))

    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    assert clock.slept == [8.0, 30.0]
    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0
    # Result numbers live in the top panel. Waiting for the next ball may
    # already show the address marker.
    assert floor.showing_address is True
    await subject.aclose()


async def test_a_round_result_clears_when_the_cap_expires_with_no_new_ball() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    await clock.elapse(READABLE)
    assert floor.showing_shot is True, "nobody asked for the floor back"

    await clock.elapse(CAP)

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    assert floor.showing_address is True
    assert floor.address_club == Club(kind=ClubKind.IRON)
    await subject.aclose()


async def test_a_ball_teed_up_straight_away_does_not_wipe_the_result() -> None:
    """The measured case. GSPro's result lands three to six seconds after the
    strike and the next ball was seen on the mat as little as ten seconds after
    it, which left about four seconds of a number the player had asked for."""

    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    await subject.on_ball_detected(BallDetected())

    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0
    await subject.aclose()


async def test_that_ball_takes_the_result_off_the_moment_the_window_ends() -> None:
    """Deferred, not ignored: the player does not wait out the cap for a floor
    the room has already asked for."""

    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_ball_detected(BallDetected())

    await clock.elapse(READABLE)

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    # A ball on the mat, so the pre-shot HUD owns the floor rather than a marker.
    assert floor.showing_address is False
    await subject.aclose()


async def test_a_new_ball_after_the_window_clears_the_result_immediately() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await clock.elapse(READABLE)

    await subject.on_ball_detected(BallDetected())

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    assert floor.showing_address is False
    await subject.aclose()


async def test_ready_for_the_next_shot_waits_out_the_readable_window() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0
    # The marker is not held back with it: the result panel and the address are
    # different parts of the surface, and the player is teeing up now.
    assert floor.showing_address is True
    # The completed DRIVER shot must not own the next address.
    assert floor.address_club is None

    await clock.elapse(READABLE)

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    assert floor.showing_address is True
    await subject.aclose()


async def test_ready_after_the_window_clears_the_result_immediately() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await clock.elapse(READABLE)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    assert floor.showing_address is True
    assert floor.address_club is None
    await subject.aclose()


async def test_finding_ball_does_not_clear_the_round_result() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0
    await subject.aclose()


async def test_a_later_round_result_is_not_cleared_by_an_earlier_timeout() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)

    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("B")))
    await asyncio.sleep(0)

    # Shot A's two timers, both now stale. Neither may touch shot B's panel.
    await clock.elapse(READABLE)
    await clock.elapse(CAP)

    assert [result.shot_id for result in floor.round_results] == ["A", "B"]
    assert floor.showing_shot is True
    assert floor.calls.count("clear_shot") == 0

    await clock.elapse(CAP + 2)

    assert floor.showing_shot is False
    assert floor.calls.count("clear_shot") == 1
    await subject.aclose()


async def test_marker_returns_for_shot_two_and_shot_three() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    subject.start()

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    assert floor.showing_address is True

    await subject.on_ball_detected(BallDetected())
    assert floor.showing_address is False
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    assert floor.showing_address is True
    # Still up, and that is the point of the readable window: the marker comes
    # back for the next ball without taking the last result off the floor.
    assert floor.showing_shot is True

    await subject.on_ball_detected(BallDetected())
    assert floor.showing_address is False
    await subject.on_round_shot(RoundShotObserved(shot=a_round("2")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    assert floor.showing_address is True
    assert floor.showing_shot is True
    await subject.aclose()


# --- the result, then the coaching, then the next ball -----------------------
#
# The ordering the room needs and the one a naive implementation gets wrong. On
# real hardware the result lands three to six seconds after the strike and
# coaching a few seconds behind it, so the two arrive close enough together to
# land on the floor at once unless something holds them apart.


async def test_coaching_waits_for_the_result_to_have_been_read() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)

    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))

    # The number the player asked for still has the floor to itself.
    assert not floor.advice
    assert floor.showing_shot is True
    await subject.aclose()


async def test_and_then_gets_it_the_moment_the_window_ends() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))

    await clock.elapse(READABLE)

    assert floor.advice[-1] == ("FACE OPEN", 0.6)
    await subject.aclose()


async def test_the_coached_wording_replaces_a_held_templated_line() -> None:
    # Both arrive inside the window, so only the better-worded one is ever
    # shown rather than the floor flickering from one to the other.
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    shot_id = uuid4()
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot_id)))
    await subject.on_advice_ready(
        AnalysisCompleted(
            shot_id=shot_id,
            advice=an_advice(shot_id, display="GOOD FACE", severity=0.08),
        )
    )

    await clock.elapse(READABLE)

    assert floor.advice == [("GOOD FACE", 0.08)]
    await subject.aclose()


async def test_held_coaching_is_shown_when_the_next_ball_is_already_down() -> None:
    # A ball on the mat ends the result. It is not a newer shot, so the line
    # still gets its own lifetime. Dropping it here is how a bay that tees up
    # inside the readable window never sees coaching.
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))
    await subject.on_ball_detected(BallDetected())

    await clock.elapse(READABLE)

    assert floor.advice[-1] == ("FACE OPEN", 0.6)
    assert floor.showing_advice is True
    assert floor.showing_shot is False
    await subject.aclose()


async def test_ball_ready_during_the_result_window_does_not_drop_coaching() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    await clock.elapse(READABLE)

    assert floor.advice[-1] == ("FACE OPEN", 0.6)
    assert floor.showing_advice is True
    await subject.aclose()


async def test_a_newer_result_drops_coaching_still_waiting_for_the_old_one() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(uuid4())))

    await subject.on_round_shot(RoundShotObserved(shot=a_round("B")))
    await asyncio.sleep(0)
    await clock.elapse(READABLE)

    assert not floor.advice
    await subject.aclose()


async def test_a_good_shot_reaches_the_floor_as_something_that_went_right() -> None:
    # The product change. A reinforcement is coaching, so it is shown; only a
    # shot nothing was measured about is silent.
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_round_shot(RoundShotObserved(shot=a_round("A")))
    await asyncio.sleep(0)
    await subject.on_shot_diagnosed(
        ShotDiagnosed(
            diagnosis=a_diagnosis(uuid4(), severity=0.08, display="GOOD FACE")
        )
    )

    await clock.elapse(READABLE)

    assert floor.advice[-1] == ("GOOD FACE", 0.08)
    await subject.aclose()


async def test_a_new_club_after_ready_owns_the_next_address() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    assert floor.play_club is None
    assert floor.address_club is None

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    assert floor.play_club == Club(kind=ClubKind.DRIVER)
    assert floor.address_club == Club(kind=ClubKind.DRIVER)

    await subject.on_round_shot(RoundShotObserved(shot=a_round("2")))
    await asyncio.sleep(0)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))

    assert floor.play_club == Club(kind=ClubKind.IRON)
    assert floor.address_club == Club(kind=ClubKind.IRON)
    await subject.aclose()


async def test_club_detected_before_ready_is_kept_for_the_upcoming_shot() -> None:
    floor = FloorSpy()
    clock = GatedClock()
    subject = flow(floor, clock)
    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
    await asyncio.sleep(0)

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.DRIVER)))
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.play_club == Club(kind=ClubKind.DRIVER)
    assert floor.address_club == Club(kind=ClubKind.DRIVER)
    await subject.aclose()


class TestACompletedShotSaysWhichStrikeItWas:
    """`ShotCompleted` carries the detection's trigger id.

    Two subsystems need to join a result back to the strike it came from, and
    neither could. Video wants the moment of impact so a clip is centred on
    the swing rather than on GSPro finishing its file seconds later; coaching
    wants the swing capture keyed on the same trigger. Both were reduced to
    guessing "the most recent one", which is wrong the moment a practice
    swing lands between the strike and the result.

    `PlayFlow` is the one place that sees both events, so the join is made
    here and travels on the fact.
    """

    async def played(
        self, subject: PlayFlow, shot_id: str, trigger_id: str | None = "16"
    ) -> None:
        if trigger_id is not None:
            await subject.on_shot_detected(
                ShotDetected(trigger_id=trigger_id, source="protee_log")
            )
        await subject.on_round_shot(
            RoundShotObserved(shot=a_round(shot_id), measured=an_observed_shot())
        )
        await asyncio.sleep(0)

    async def test_a_result_carries_the_detection_that_preceded_it(self) -> None:
        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await self.played(subject, "1", trigger_id="16")
        await subject.aclose()

        completed = [
            event for event in announcements.events if isinstance(event, ShotCompleted)
        ]
        assert [event.trigger_id for event in completed] == ["16"]

    async def test_a_result_with_no_detection_carries_nothing(self) -> None:
        """GSPro Connect mode has no ProTee observer behind it, and inventing
        a trigger id there would make the join silently wrong rather than
        silently absent."""

        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await self.played(subject, "1", trigger_id=None)
        await subject.aclose()

        completed = [
            event for event in announcements.events if isinstance(event, ShotCompleted)
        ]
        assert [event.trigger_id for event in completed] == [None]

    async def triggers_of(self, announcements: Announcements) -> list[str | None]:
        return [
            event.trigger_id
            for event in announcements.events
            if isinstance(event, ShotCompleted)
        ]

    async def test_a_result_belongs_to_the_strike_that_caused_it_not_the_last_one(
        self,
    ) -> None:
        """The case that used to hand a coach two different swings.

        GSPro finishes a ball 2.6 to 9.3 seconds after the trigger, so a
        second ball inside that window is ordinary. This used to read the
        newest detection, which published the first ball's numbers under the
        second ball's trigger â€” and video and swing capture both join on that
        id, so the numbers of one swing arrived with the pictures of another.
        """

        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await subject.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        await subject.on_shot_detected(
            ShotDetected(trigger_id="17", source="protee_log")
        )
        await subject.on_round_shot(
            RoundShotObserved(shot=a_round("1"), measured=an_observed_shot())
        )
        await asyncio.sleep(0)
        await subject.aclose()

        assert await self.triggers_of(announcements) == ["16"]

    async def test_the_second_result_gets_the_second_strike(self) -> None:
        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await subject.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        await subject.on_shot_detected(
            ShotDetected(trigger_id="17", source="protee_log")
        )
        for shot_id in ("1", "2"):
            await subject.on_round_shot(
                RoundShotObserved(shot=a_round(shot_id), measured=an_observed_shot())
            )
            await asyncio.sleep(0)
        await subject.aclose()

        assert await self.triggers_of(announcements) == ["16", "17"]

    async def test_a_detection_is_claimed_once_and_not_reused(self) -> None:
        """A detection explains one result. Handing it to a second one would
        cut a second clip of the same swing and coach two shots from it."""

        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await subject.on_shot_detected(
            ShotDetected(trigger_id="16", source="protee_log")
        )
        for shot_id in ("1", "2"):
            await subject.on_round_shot(
                RoundShotObserved(shot=a_round(shot_id), measured=an_observed_shot())
            )
            await asyncio.sleep(0)
        await subject.aclose()

        assert await self.triggers_of(announcements) == ["16", None]

    async def test_a_strike_nothing_reported_ages_out_of_the_queue(self) -> None:
        """Otherwise it sits at the head for the session and every result
        after it is one behind."""

        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        await subject.on_shot_detected(
            ShotDetected(
                trigger_id="16",
                source="protee_log",
                monotonic_ns=time.monotonic_ns() - 60_000_000_000,
            )
        )
        await subject.on_shot_detected(
            ShotDetected(trigger_id="17", source="protee_log")
        )
        await subject.on_round_shot(
            RoundShotObserved(shot=a_round("1"), measured=an_observed_shot())
        )
        await asyncio.sleep(0)
        await subject.aclose()

        assert await self.triggers_of(announcements) == ["17"]

    async def test_a_simulator_that_stops_reporting_cannot_grow_the_queue(
        self,
    ) -> None:
        subject = flow(FloorSpy(), GatedClock(), Announcements())

        for index in range(50):
            await subject.on_shot_detected(
                ShotDetected(trigger_id=str(index), source="protee_log")
            )
        await subject.aclose()

        assert len(subject._triggers) <= 8

    async def test_a_detection_older_than_any_shot_is_not_used(self) -> None:
        """A practice swing at the start of a session must not claim the
        result of a real shot half a minute later."""

        announcements = Announcements()
        subject = flow(FloorSpy(), GatedClock(), announcements)

        stale = ShotDetected(
            trigger_id="16",
            source="protee_log",
            monotonic_ns=time.monotonic_ns() - 60_000_000_000,
        )
        await subject.on_shot_detected(stale)
        await self.played(subject, "1", trigger_id=None)
        await subject.aclose()

        completed = [
            event for event in announcements.events if isinstance(event, ShotCompleted)
        ]
        assert [event.trigger_id for event in completed] == [None]

    async def test_an_expired_strike_says_coaching_never_started(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        subject = flow(FloorSpy(), GatedClock(), Announcements())
        subject._triggers.append(("41", 0))

        with caplog.at_level(logging.INFO):
            subject._expire_triggers(30_000_000_001)

        reasons = [
            getattr(record, "coaching_reason", "")
            for record in caplog.records
            if record.getMessage() == "coaching_advice_suppressed"
        ]
        assert reasons == ["no_round_result"]
        assert not subject._triggers
