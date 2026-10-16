"""The floor as independent layers, driven through the real presenter.

The bugs this file exists for were all one shape: a decision about one layer
reaching into another. The turf light went out because the marker was hidden;
the device's own caption sat on top of the result because both were drawn and
neither knew about the other; the pre-shot numbers appeared only once a ball had
been detected, which is after the club has been chosen.

So these are written against `FloorState` rather than against a spy. A spy can
only say what was called. What matters here is what ends up on the surface at
the same time as what else.
"""

import asyncio

import pytest

from caddiej.domain.bag import ClubDistanceProfile, StaticClubDistances
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RoundShotObserved,
    ShotDispatched,
    TargetDistanceObserved,
)
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.play import LOCK_ACK_SECONDS, PlayFlow
from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import PhysicalPoint, PhysicalRect
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorState

pytestmark = pytest.mark.asyncio

SEVEN_IRON = Club(kind=ClubKind.IRON, vendor_code="7 Iron")

BAG = ClubDistanceProfile.from_carries(
    {"DR": 220.0, "5I": 155.0, "7I": 135.0, "PW": 98.0, "SW": 70.0}
)


class GatedClock:
    """One gate per sleep, so a window can be expired without its cap."""

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


# A round result arms the readable window first and the cap second.
READABLE = 0


async def _publish(_event: object) -> None:
    return None


def bay(
    *,
    filled: bool = True,
    bag: ClubDistanceProfile | None = None,
    clock: GatedClock | None = None,
) -> tuple[FloorService, PlayFlow]:
    """A floor wired the way `app.py` wires one, with time under test control."""

    floor = FloorService()
    presenter = FloorPresenter(
        floor,
        layout_from_tees(
            PhysicalRect(x_m=0.5, y_m=0.72, width_m=0.30, depth_m=0.45),
            tee=PhysicalPoint(x_m=0.5, y_m=0.72),
            strip_filled=filled,
        ),
    )
    return floor, PlayFlow(
        floor=presenter,
        publish=_publish,
        sleep=(clock or GatedClock()).sleep,
        club_distances=StaticClubDistances(carries=bag or ClubDistanceProfile()),
    )


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(
        shot_id=shot_id,
        total_distance_raw=35.46979,
        distance_to_pin_raw=16.7867889,
    )


def lit(state: FloorState) -> bool:
    strip = state.hitting_strip
    return strip is not None and strip.visible and strip.filled


# --- Layer A: the turf light ----------------------------------------------


class TestTheHittingStripStaysLit:
    """FLOOR_HITTING_STRIP_FILL is a statement about a room, not about a shot.

    On hardware the pad went dark the moment ProTee went READY with a ball on
    the mat, because lighting it was something the address marker did on its way
    past. It is now lit once and left alone.
    """

    async def test_it_is_lit_as_soon_as_the_floor_starts(self) -> None:
        floor, subject = bay()

        subject.start()

        assert lit(floor.state) is True
        await subject.aclose()

    async def test_ready_leaves_it_lit(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert floor.state.play_status == "READY"
        assert lit(floor.state) is True
        await subject.aclose()

    async def test_finding_ball_leaves_it_lit(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.play_status == "FINDING BALL"
        assert lit(floor.state) is True
        await subject.aclose()

    async def test_a_ball_on_the_mat_leaves_it_lit(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_ball_detected(BallDetected())

        assert floor.state.marker.visible is False
        assert lit(floor.state) is True
        await subject.aclose()

    async def test_a_shot_being_sent_leaves_it_lit(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_shot_dispatched(ShotDispatched())

        assert lit(floor.state) is True
        await subject.aclose()

    async def test_a_final_result_leaves_it_lit(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)

        assert floor.state.round_result is not None
        assert lit(floor.state) is True
        await subject.aclose()

    async def test_a_whole_shot_cycle_never_puts_it_out(self) -> None:
        """The sequence from the bay, start to finish, checked at every step."""

        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()
        seen = [lit(floor.state)]

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        seen.append(lit(floor.state))
        await subject.on_club_selected(ClubSelected(club=SEVEN_IRON))
        seen.append(lit(floor.state))
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
        seen.append(lit(floor.state))
        await subject.on_ball_detected(BallDetected())
        seen.append(lit(floor.state))
        await subject.on_shot_dispatched(ShotDispatched())
        seen.append(lit(floor.state))
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))
        seen.append(lit(floor.state))
        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        seen.append(lit(floor.state))
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=16.8))
        seen.append(lit(floor.state))
        await clock.elapse_duration(8.0)
        seen.append(lit(floor.state))

        assert seen == [True] * len(seen)
        await subject.aclose()

    async def test_shutting_down_is_the_one_thing_that_puts_it_out(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.aclose()

        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is False
        # The geometry survives, so a restart fades the same pad back in.
        assert floor.state.hitting_strip.filled is True

    async def test_an_unfilled_strip_is_still_a_persistent_outline(self) -> None:
        floor, subject = bay(filled=False)
        subject.start()

        await subject.on_ball_detected(BallDetected())

        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is True
        assert floor.state.hitting_strip.filled is False
        await subject.aclose()


# --- the device's own words -----------------------------------------------


class TestReadyAndFindingBallAreNotPlayerFacing:
    """The device's words stay in the domain, because the marker and the
    information area both branch on them. They are not what the strip says.
    READY is drawn as BALL READY; FINDING BALL is drawn as PLACE BALL, on the
    rear edge, not over the result."""

    async def test_ready_is_known_and_the_strip_says_ball_ready(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert subject.ready is True
        assert floor.state.play_status == "READY"
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "BALL READY"
        assert floor.state.ball_status.ready is True
        assert "FINDING BALL" not in _player_facing_text(floor.state)
        await subject.aclose()

    async def test_finding_ball_is_known_and_the_strip_says_place_ball(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.play_status == "FINDING BALL"
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.ball_status.ready is False
        assert "FINDING BALL" not in _player_facing_text(floor.state)
        await subject.aclose()

    async def test_finding_ball_does_not_share_the_floor_with_a_result(self) -> None:
        """The reported overlap: FINDING BALL was drawn over the shot numbers,
        because the caption and the result panel were two layers that did not
        know about each other. PLACE BALL stays on the rear edge; the numbers
        stay where they were."""

        floor, subject = bay()
        subject.start()
        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.round_result is not None
        assert floor.state.round_result.shot_distance_display == "35 m"
        assert "FINDING BALL" not in _player_facing_text(floor.state)
        assert {"35 m", "17 m", "PLACE BALL"} <= _player_facing_text(floor.state)
        await subject.aclose()


def _player_facing_text(state: FloorState) -> set[str]:
    """Every string the overlay actually draws, from one snapshot.

    `play_status` and `club_label` are deliberately absent: the overlay writes
    them to data attributes and keeps the element hidden. The chamfer's label
    is drawn, and it is not those words. `AddressView`'s `club_display` is
    absent for the same reason as the device status.
    """

    drawn = {state.status_text}
    if state.ball_status is not None:
        drawn.add(state.ball_status.label)
    if state.advice is not None:
        drawn.add(state.advice.text)
    if state.flourish is not None:
        drawn.add(state.flourish.label)
    if state.address is not None:
        drawn.add(state.address.distance_display or "")
        drawn.add(state.address.recommended_club_display)
    if state.round_result is not None:
        drawn.add(state.round_result.shot_distance_display or "")
        drawn.add(state.round_result.remaining_distance_display or "")
    return drawn - {""}


# --- Layers B and C: one information area ---------------------------------


class TestResultAndAddressNeverShareTheFloor:
    async def test_the_result_owns_the_area_while_it_is_guaranteed_readable(
        self,
    ) -> None:
        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
        assert floor.state.address is not None

        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        # GSPro's next target lands about half a second after the result does.
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=16.8))

        assert floor.state.round_result is not None
        assert floor.state.address is None
        await subject.aclose()

    async def test_the_address_resumes_when_the_window_ends(self) -> None:
        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()
        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        await clock.elapse(READABLE)

        assert floor.state.round_result is None
        assert floor.state.address is not None
        assert floor.state.address.distance_display == "147 m"
        assert floor.state.address.recommended_club_display == "5I"
        await subject.aclose()

    async def test_at_no_point_in_a_cycle_are_both_on_the_floor(self) -> None:
        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()
        overlaps = []

        def note() -> None:
            overlaps.append(
                floor.state.address is not None and floor.state.round_result is not None
            )

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
        note()
        await subject.on_ball_detected(BallDetected())
        note()
        await subject.on_shot_dispatched(ShotDispatched())
        note()
        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        note()
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=16.8))
        note()
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        note()
        await clock.elapse_duration(8.0)
        note()

        assert overlaps == [False] * len(overlaps)
        await subject.aclose()

    async def test_the_marker_runs_on_its_own_lifecycle_throughout(self) -> None:
        """Layer D is not affected by any of the above: the ball marker follows
        ball detection and readiness, and a result on the floor does not hold it
        back."""

        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        assert floor.state.marker.visible is True
        await subject.on_ball_detected(BallDetected())
        assert floor.state.marker.visible is False

        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert floor.state.round_result is not None
        assert floor.state.marker.visible is True
        await subject.aclose()


# --- Layer B: what it says -------------------------------------------------


class TestWhatTheAddressSays:
    async def test_a_target_and_a_bag_give_a_distance_and_a_club(self) -> None:
        floor, subject = bay(bag=BAG)
        subject.start()
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))

        assert floor.state.address is not None
        assert floor.state.address.distance_display == "147 m"
        assert floor.state.address.recommended_club_display == "5I"
        await subject.aclose()

    async def test_no_bag_shows_the_distance_alone(self) -> None:
        floor, subject = bay()
        subject.start()
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))

        assert floor.state.address is not None
        assert floor.state.address.distance_display == "147 m"
        assert floor.state.address.recommended_club_display == ""
        await subject.aclose()

    async def test_no_target_shows_no_panel_even_with_a_bag(self) -> None:
        """A club needs a distance to have been chosen for. Without one there is
        nothing to say, and nothing is what the floor says."""

        floor, subject = bay(bag=BAG)
        subject.start()

        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        await subject.on_club_selected(ClubSelected(club=SEVEN_IRON))

        assert floor.state.address is None
        await subject.aclose()

    async def test_the_shot_takes_its_own_target_with_it(self) -> None:
        """The distance the player just played *from* must not stand under a
        label that says how far is left. GSPro sends the next one when it has
        finished flying the ball; until then CaddieJ knows no current target."""

        floor, subject = bay(bag=BAG)
        subject.start()
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))

        await subject.on_shot_dispatched(ShotDispatched())
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.address is None
        await subject.aclose()

    async def test_the_next_target_is_the_new_one_not_the_last_result(self) -> None:
        """Both numbers exist at the same moment and they are not the same fact.
        `16.8` is what GSPro says is left to play; `17 m` under TO PIN in the
        result panel is what was left once the last shot came to rest. The
        address panel must be showing the first because the first arrived, not
        because the second was to hand."""

        clock = GatedClock()
        floor, subject = bay(bag=BAG, clock=clock)
        subject.start()
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))
        await subject.on_shot_dispatched(ShotDispatched())
        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)
        assert floor.state.round_result is not None
        assert floor.state.round_result.remaining_distance_display == "17 m"

        await subject.on_target_distance(TargetDistanceObserved(distance_raw=98.4))
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        await clock.elapse(READABLE)

        assert floor.state.address is not None
        assert floor.state.address.distance_display == "98 m"
        assert floor.state.address.recommended_club_display == "PW"
        await subject.aclose()

    async def test_a_dropped_link_forgets_the_target_rather_than_keeping_it(
        self,
    ) -> None:
        floor, subject = bay(bag=BAG)
        subject.start()
        await subject.on_target_distance(TargetDistanceObserved(distance_raw=147.2))

        await subject.on_link_changed(
            LaunchMonitorLinkChanged(state=LinkState.SIMULATOR_UNAVAILABLE)
        )

        assert floor.state.address is None
        await subject.aclose()


# --- ball lock acknowledgement -----------------------------------------------


class TestBallLockAcknowledgement:
    """One pulse on absent -> present. The turf light underneath never goes out."""

    async def test_absent_to_present_produces_exactly_one_acknowledgement(
        self,
    ) -> None:
        clock = GatedClock()
        floor, subject = bay(clock=clock)
        subject.start()
        corners = floor.state.hitting_strip.corners if floor.state.hitting_strip else ()

        await subject.on_ball_detected(BallDetected())
        await asyncio.sleep(0)

        assert floor.state.lock_acknowledgement is not None
        assert floor.state.lock_acknowledgement.seconds == LOCK_ACK_SECONDS
        assert floor.state.marker.visible is False
        assert lit(floor.state) is True
        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.corners == corners
        await subject.aclose()

    async def test_repeated_present_observations_do_not_retrigger(self) -> None:
        clock = GatedClock()
        floor, subject = bay(clock=clock)
        subject.start()
        await subject.on_ball_detected(BallDetected())
        await asyncio.sleep(0)
        version = floor.state.version

        await subject.on_ball_detected(BallDetected())
        await subject.on_ball_detected(BallDetected())

        assert floor.state.lock_acknowledgement is not None
        assert floor.state.version == version
        assert clock.slept == [LOCK_ACK_SECONDS]
        await subject.aclose()

    async def test_the_acknowledgement_expires_and_the_strip_stays_lit(self) -> None:
        clock = GatedClock()
        floor, subject = bay(clock=clock)
        subject.start()
        await subject.on_ball_detected(BallDetected())
        await asyncio.sleep(0)

        await clock.elapse_duration(LOCK_ACK_SECONDS)

        assert floor.state.lock_acknowledgement is None
        assert lit(floor.state) is True
        await subject.aclose()

    async def test_a_cleared_then_re_detected_ball_can_acknowledge_again(self) -> None:
        clock = GatedClock()
        floor, subject = bay(clock=clock)
        subject.start()
        await subject.on_ball_detected(BallDetected())
        await asyncio.sleep(0)
        await clock.elapse_duration(LOCK_ACK_SECONDS)
        assert floor.state.lock_acknowledgement is None

        await subject.on_ball_cleared(BallCleared())
        await subject.on_ball_detected(BallDetected())
        await asyncio.sleep(0)

        assert floor.state.lock_acknowledgement is not None
        await clock.elapse_duration(LOCK_ACK_SECONDS)
        assert floor.state.lock_acknowledgement is None
        assert lit(floor.state) is True
        await subject.aclose()


# --- what the result says -----------------------------------------------------


class TestWhatTheResultSays:
    async def test_shot_and_remaining_distance_render_without_ball_speed(
        self,
    ) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_round_shot(RoundShotObserved(shot=a_round("1")))
        await asyncio.sleep(0)

        result = floor.state.round_result
        assert result is not None
        assert result.shot_distance_display == "35 m"
        assert result.remaining_distance_display == "17 m"
        assert result.ball_speed_display is None
        assert "km/h" not in _player_facing_text(floor.state)
        assert _player_facing_text(floor.state) == {"35 m", "17 m", "PLACE BALL"}
        await subject.aclose()

    async def test_a_result_without_remaining_distance_omits_that_figure(
        self,
    ) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_round_shot(
            RoundShotObserved(
                shot=ObservedRoundShot(shot_id="1", total_distance_raw=35.4)
            )
        )
        await asyncio.sleep(0)

        result = floor.state.round_result
        assert result is not None
        assert result.shot_distance_display == "35 m"
        assert result.remaining_distance_display is None
        assert _player_facing_text(floor.state) == {"35 m", "PLACE BALL"}
        await subject.aclose()

    async def test_a_result_without_shot_distance_omits_that_figure(self) -> None:
        floor, subject = bay()
        subject.start()

        await subject.on_round_shot(
            RoundShotObserved(
                shot=ObservedRoundShot(shot_id="1", distance_to_pin_raw=16.8)
            )
        )
        await asyncio.sleep(0)

        result = floor.state.round_result
        assert result is not None
        assert result.shot_distance_display is None
        assert result.remaining_distance_display == "17 m"
        assert _player_facing_text(floor.state) == {"17 m", "PLACE BALL"}
        await subject.aclose()
