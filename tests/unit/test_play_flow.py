"""What the floor shows as a shot is played.

The floor is a spy here rather than a real one: this is about the sequence of
decisions, and the drawing of them is `test_floor_presenter`'s subject. Time is
fake, so nothing waits for a shot display to expire.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
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
    ShotCompleted,
    ShotDiagnosed,
    ShotIngested,
)
from caddiej.domain.play import PlayFlow
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
        self.address_club: Club | None = None
        self.shots: list[Shot] = []
        self.advice: list[tuple[str, float]] = []
        self.showing_address = False
        self.showing_shot = False
        self.showing_advice = False

    def show_address(self, club: Club | None) -> None:
        self.calls.append("show_address")
        self.address_club = club
        self.showing_address = True

    def hide_address(self) -> None:
        self.calls.append("hide_address")
        self.showing_address = False

    def show_shot(self, shot: Shot) -> None:
        self.calls.append("show_shot")
        self.shots.append(shot)
        self.showing_shot = True

    def clear_shot(self) -> None:
        self.calls.append("clear_shot")
        self.showing_shot = False

    def show_advice(self, text: str, severity: float) -> None:
        self.calls.append("show_advice")
        self.advice.append((text, severity))
        self.showing_advice = True

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
) -> PlayFlow:
    return PlayFlow(
        floor=floor,
        publish=(announcements or Announcements()).publish,
        shot_display_seconds=6.0,
        advice_display_seconds=9.0,
        sleep=(clock or Clock()).sleep,
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


async def test_removing_the_ball_brings_the_marker_back() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_ball_cleared(BallCleared())

    assert floor.showing_address is True


async def test_changing_club_while_a_ball_is_in_place_shows_no_marker() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_ball_detected(BallDetected())

    await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))

    assert floor.showing_address is False


async def test_a_device_that_says_it_is_not_ready_gets_no_marker() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    subject.start()

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    assert floor.showing_address is False


async def test_a_device_becoming_ready_brings_the_marker_back() -> None:
    floor = FloorSpy()
    subject = flow(floor)
    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

    await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

    assert floor.showing_address is True


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


async def test_the_coaching_line_has_its_own_lifetime() -> None:
    # Longer than the shot's, and started when the line arrives rather than when
    # the shot did: a model answering four seconds late should still get a full
    # showing rather than two seconds of what is left of the shot's.
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await asyncio.sleep(0)

    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    assert clock.slept == [6.0, 9.0]
    await subject.aclose()


async def test_the_coached_wording_restarts_the_reading_time() -> None:
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

    assert clock.slept == [6.0, 9.0, 9.0]
    assert floor.showing_advice is True
    await subject.aclose()


async def test_the_coaching_line_comes_down_once_it_has_been_read() -> None:
    floor = FloorSpy()
    clock = Clock()
    subject = flow(floor, clock)
    shot = a_shot()
    await subject.on_shot(ShotIngested(shot=shot))
    await subject.on_shot_diagnosed(ShotDiagnosed(diagnosis=a_diagnosis(shot.shot_id)))
    await asyncio.sleep(0)

    await clock.elapse()

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
