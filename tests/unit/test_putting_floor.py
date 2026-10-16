"""Putting mode, and the line CaddieJ will and will not draw after a putt.

Two subjects.

The first is presentation: a putter puts the floor into a different state — its
own ball position, a lane, and none of the full-swing furniture — and taking the
putter out of the player's hands takes all of that away again.

The second is harder and is the reason this file is careful. GSPro's
`activeShot.sd.path` is a rolling buffer of the *tail* of a trajectory, and on
the real capture it reaches back past the start of a short shot into the ball
being placed on the green. A line on a floor is read as a measurement whether or
not it is one, so the rule is: draw the roll when the buffer demonstrably holds
the whole of it and nothing else, and otherwise draw nothing and say why. The
refusals below are each a shape that actually occurs in `currentRound.dat`.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from caddiej.domain.events import (
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    RoundShotObserved,
)
from caddiej.domain.observation import BallPathPoint, ObservedRoundShot, WorldPoint
from caddiej.domain.play import PlayFlow
from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import (
    PhysicalPoint,
    PhysicalRect,
    rectangular_calibration,
)
from caddiej.floor.layout import FloorLayout, PhysicalOffset, PlacementMode
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.putting import MIN_ROLL_M, PuttTraceRefused, putt_trace
from caddiej.floor.service import FloorService
from caddiej.floor.state import MarkerVariant, PuttPath

STRIP = PhysicalRect(x_m=2.0, y_m=2.2, width_m=0.30, depth_m=0.45)
PUTTER_BALL = PhysicalPoint(x_m=2.0, y_m=1.80)
# A real 4 × 3 m bay rather than the unit square, so nothing here can pass by
# accident on a floor where metres and surface coordinates are the same number.
CALIBRATION = rectangular_calibration(4.0, 3.0)


def surface(x_m: float, y_m: float) -> tuple[float, float]:
    point = CALIBRATION.to_surface(PhysicalPoint(x_m=x_m, y_m=y_m))
    return point.x, point.y


def a_layout(*, lane: bool = True) -> FloorLayout:
    return FloorLayout(
        strip=STRIP,
        ball={
            PlacementMode.IRON: PhysicalOffset(),
            PlacementMode.PUTTER: PhysicalOffset(dy_m=-0.40),
        },
        putting_lane=lane,
        putting_lane_length_m=0.80,
        putting_lane_width_m=0.24,
    )


def a_presenter(floor: FloorService, *, lane: bool = True) -> FloorPresenter:
    return FloorPresenter(
        floor,
        club_path_length=0.4,
        calibration=CALIBRATION,
        layout=a_layout(lane=lane),
    )


# --- the data --------------------------------------------------------------


def straight_roll(
    length_m: float = 2.4, samples: int = 12, drift_m: float = 0.0
) -> ObservedRoundShot:
    """A putt whose path buffer holds the whole roll and nothing else.

    Built in GSPro's own frame: +z down-range, y up. The ball starts at the
    origin and finishes `length_m` away, drifting `drift_m` to one side on the
    way, which is what a break looks like in this data.
    """

    start = WorldPoint(x=0.0, y=0.0, z=0.0)
    end = WorldPoint(x=drift_m, y=0.0, z=length_m)
    path = tuple(
        BallPathPoint(
            time=index * 0.1,
            x=drift_m * (index / (samples - 1)) ** 2,
            y=0.0,
            z=length_m * index / (samples - 1),
        )
        for index in range(samples)
    )
    return ObservedRoundShot(
        shot_id="putt",
        starting_position=start,
        ending_position=end,
        ball_path=path,
        total_distance_raw=length_m,
    )


def a_round(shot_id: str) -> ObservedRoundShot:
    return ObservedRoundShot(
        shot_id=shot_id, ball_speed_raw=32.8386, total_distance_raw=28.0288715
    )


class Clock:
    def __init__(self) -> None:
        self._gates: list[asyncio.Event] = []

    async def sleep(self, _seconds: float) -> None:
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()


async def _publish(_event: object) -> None:
    return None


def a_flow(floor: FloorService) -> PlayFlow:
    return PlayFlow(
        floor=a_presenter(floor),
        publish=_publish,
        shot_result_min_duration=8.0,
        shot_result_max_duration=30.0,
        sleep=Clock().sleep,
    )


# --- putting mode ----------------------------------------------------------


@pytest.mark.asyncio
class TestThePuttingFloor:
    """What a putter changes, and what it leaves alone."""

    async def test_a_putter_gets_its_own_ball_position_and_a_lane(self) -> None:
        floor = FloorService()
        subject = a_flow(floor)
        subject.start()

        await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))

        assert floor.state.marker.variant is MarkerVariant.PUTTER
        marker = floor.state.marker.position
        assert (marker.x, marker.y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m)
        )
        lane = floor.state.putting_lane
        assert lane is not None and lane.visible is True
        await subject.aclose()

    async def test_the_lane_runs_down_range_from_the_putter_ball(self) -> None:
        floor = FloorService()
        a_presenter(floor).show_putting_guide(Club(kind=ClubKind.PUTTER))

        lane = floor.state.putting_lane
        assert lane is not None
        assert (lane.start.x, lane.start.y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m)
        )
        # Down-range is −Y in metres, so the far end has the smaller coordinate.
        assert (lane.end.x, lane.end.y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m - 0.80)
        )

    async def test_the_lane_moves_with_the_hitting_strip(self) -> None:
        floor = FloorService()
        FloorPresenter(
            floor,
            club_path_length=0.4,
            calibration=CALIBRATION,
            layout=FloorLayout(
                strip=PhysicalRect(x_m=2.5, y_m=2.2, width_m=0.3, depth_m=0.45),
                ball={PlacementMode.PUTTER: PhysicalOffset(dy_m=-0.40)},
                putting_lane_length_m=0.80,
            ),
        ).show_putting_guide(Club(kind=ClubKind.PUTTER))

        lane = floor.state.putting_lane
        assert lane is not None
        assert (lane.start.x, lane.start.y) == pytest.approx(surface(2.5, 1.80))

    @pytest.mark.parametrize(
        "kind", [ClubKind.IRON, ClubKind.DRIVER, ClubKind.WEDGE, ClubKind.UNKNOWN]
    )
    async def test_no_other_club_gets_a_lane(self, kind: ClubKind) -> None:
        floor = FloorService()

        a_presenter(floor).show_putting_guide(Club(kind=kind))

        assert floor.state.putting_lane is None

    async def test_a_bay_can_turn_the_lane_off_and_keep_the_ball_position(self) -> None:
        floor = FloorService()
        subject = a_presenter(floor, lane=False)

        subject.show_putting_guide(Club(kind=ClubKind.PUTTER))
        subject.show_address(Club(kind=ClubKind.PUTTER))

        assert floor.state.putting_lane is None
        assert floor.state.marker.variant is MarkerVariant.PUTTER

    async def test_the_lane_does_not_churn_the_state_while_a_putter_is_in_play(
        self,
    ) -> None:
        # The flow asks on every pass, so setting state that is already set has
        # to be free: a version bump is a snapshot to every connected browser.
        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.PUTTER))
        settled = floor.state.version

        for _ in range(5):
            subject.show_putting_guide(Club(kind=ClubKind.PUTTER))

        assert floor.state.version == settled


@pytest.mark.asyncio
class TestPuttToNormalShotAndBack:
    async def test_picking_up_an_iron_takes_the_whole_putting_floor_away(self) -> None:
        floor = FloorService()
        subject = a_flow(floor)
        subject.start()
        await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))
        await subject.on_round_shot(RoundShotObserved(shot=straight_roll()))
        await asyncio.sleep(0)
        assert floor.state.putt_path is not None

        await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))

        lane = floor.state.putting_lane
        assert lane is not None and lane.visible is False
        # The last putt's roll belongs to putting mode, not to the next full
        # swing's address.
        assert floor.state.putt_path is None
        assert floor.state.marker.variant is MarkerVariant.NORMAL
        marker = floor.state.marker.position
        assert (marker.x, marker.y) == pytest.approx(surface(STRIP.x_m, STRIP.y_m))
        await subject.aclose()

    async def test_going_back_to_the_putter_puts_the_lane_back(self) -> None:
        floor = FloorService()
        subject = a_flow(floor)
        subject.start()

        for kind in (ClubKind.PUTTER, ClubKind.IRON, ClubKind.PUTTER):
            await subject.on_club_selected(ClubSelected(club=Club(kind=kind)))

        lane = floor.state.putting_lane
        assert lane is not None and lane.visible is True
        assert floor.state.marker.variant is MarkerVariant.PUTTER
        await subject.aclose()

    async def test_a_full_swing_after_a_putt_is_presented_normally(self) -> None:
        floor = FloorService()
        subject = a_flow(floor)
        subject.start()
        await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.PUTTER)))
        await subject.on_round_shot(RoundShotObserved(shot=straight_roll()))
        await asyncio.sleep(0)

        await subject.on_club_selected(ClubSelected(club=Club(kind=ClubKind.IRON)))
        await subject.on_ball_detected(BallDetected())
        await subject.on_round_shot(RoundShotObserved(shot=a_round("2")))
        await asyncio.sleep(0)
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        result = floor.state.round_result
        assert result is not None and result.shot_distance_display == "28 m"
        assert floor.state.putt_path is None
        assert floor.state.marker.visible is True
        await subject.aclose()


# --- the roll --------------------------------------------------------------


class TestAMeasuredRollIsDrawn:
    """When the buffer really does hold the whole putt."""

    def trace(self, shot: ObservedRoundShot) -> PuttPath:
        outcome = putt_trace(shot, PUTTER_BALL, CALIBRATION)
        assert isinstance(outcome, PuttPath), outcome
        return outcome

    def test_the_trace_starts_under_the_ball_the_player_just_hit(self) -> None:
        first = self.trace(straight_roll()).points[0]

        assert (first.x, first.y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m)
        )

    def test_it_runs_down_range_for_the_distance_the_ball_rolled(self) -> None:
        last = self.trace(straight_roll(length_m=2.4)).points[-1]

        # Down-range is −Y, and the roll is 2.4 m of it.
        assert (last.x, last.y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m - 2.4)
        )

    def test_a_break_shows_as_a_break_rather_than_a_straight_line(self) -> None:
        origin, _ = surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m)

        points = self.trace(straight_roll(drift_m=0.3)).points

        sideways = [point.x - origin for point in points]
        assert max(abs(value) for value in sideways) > 0.01
        # Monotonic in one direction: a break bends, it does not wander.
        assert sideways == sorted(sideways) or sideways == sorted(
            sideways, reverse=True
        )

    def test_the_source_travels_with_the_points(self) -> None:
        # So nothing downstream has to guess how much of a measurement this is,
        # and a later, better source can be told apart from this one in a log.
        assert self.trace(straight_roll()).source == "gspro_round_path"

    def test_a_long_roll_is_thinned_without_losing_where_the_ball_stopped(self) -> None:
        trace = self.trace(straight_roll(length_m=6.0, samples=400))

        assert len(trace.points) <= 48
        assert (trace.points[-1].x, trace.points[-1].y) == pytest.approx(
            surface(PUTTER_BALL.x_m, PUTTER_BALL.y_m - 6.0)
        )


class TestWhatIsRefusedAndWhy:
    """Each of these is a shape the real `currentRound.dat` actually contains.

    A refusal carries one word an operator can grep the log for, because "why
    is there no line after my putt" is a question asked of a live bay.
    """

    def refusal(self, shot: ObservedRoundShot) -> str:
        outcome = putt_trace(shot, PUTTER_BALL)
        assert isinstance(outcome, PuttTraceRefused), "this must not be drawn"
        return outcome.reason

    def test_a_shot_with_no_path_at_all(self) -> None:
        shot = ObservedRoundShot(shot_id="1", total_distance_raw=2.0)

        assert "start or end" in self.refusal(shot)

    def test_a_path_of_two_samples_is_a_line_not_a_roll(self) -> None:
        full = straight_roll()
        shot = ObservedRoundShot(
            shot_id="1",
            starting_position=full.starting_position,
            ending_position=full.ending_position,
            ball_path=full.ball_path[:2],
        )

        assert "three path samples" in self.refusal(shot)

    def test_a_tap_in_has_no_shape_worth_drawing(self) -> None:
        assert "barely moved" in self.refusal(straight_roll(length_m=MIN_ROLL_M / 2))

    def test_a_buffer_that_starts_half_way_down_the_roll(self) -> None:
        """The commonest case, and the one that made this module necessary.

        The longest shot in the real capture travelled 101 m and carried
        thirteen samples, the first of which was already 95 m down-range.
        """

        full = straight_roll(length_m=3.0, samples=12)
        shot = ObservedRoundShot(
            shot_id="1",
            starting_position=full.starting_position,
            ending_position=full.ending_position,
            ball_path=full.ball_path[7:],
        )

        assert "does not start at the shot" in self.refusal(shot)

    def test_a_buffer_that_reaches_back_into_the_ball_being_placed(self) -> None:
        """The other real shape: the last entry in the capture moved 0.124 m
        and its path spanned 0.807 m, including a 0.17 m drop in `pos.y`."""

        full = straight_roll(length_m=0.5, samples=8)
        placing = (
            BallPathPoint(time=-0.3, x=0.0, y=0.17, z=-0.075),
            BallPathPoint(time=-0.2, x=0.0, y=0.09, z=-0.06),
            BallPathPoint(time=-0.1, x=0.0, y=0.0, z=-0.04),
        )
        shot = ObservedRoundShot(
            shot_id="1",
            starting_position=full.starting_position,
            ending_position=full.ending_position,
            ball_path=placing + full.ball_path,
        )

        assert "left the surface" in self.refusal(shot)

    def test_a_path_that_does_not_end_up_where_the_ball_did(self) -> None:
        full = straight_roll(length_m=2.0, samples=10)
        backwards = tuple(reversed(full.ball_path))
        shot = ObservedRoundShot(
            shot_id="1",
            starting_position=full.starting_position,
            ending_position=full.ending_position,
            ball_path=backwards,
        )

        assert self.refusal(shot)


class TestWhatThePresenterDoesAboutIt:
    def test_a_drawable_roll_reaches_the_floor_and_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.PUTTER))

        with caplog.at_level(logging.INFO):
            subject.show_round_result(straight_roll())

        path = floor.state.putt_path
        assert path is not None and len(path.points) >= 3
        assert "floor_putt_path_drawn" in {r.message for r in caplog.records}

    def test_a_refusal_leaves_the_floor_clean_and_says_why(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.PUTTER))

        with caplog.at_level(logging.INFO):
            subject.show_round_result(
                ObservedRoundShot(shot_id="1", total_distance_raw=2.0)
            )

        assert floor.state.putt_path is None
        named = [
            record
            for record in caplog.records
            if record.message == "floor_putt_path_unavailable"
        ]
        assert len(named) == 1
        assert named[0].floor_putt_reason

    def test_a_full_swing_is_never_given_a_putt_trace(self) -> None:
        # Even with a path buffer that would satisfy every check: this shape is
        # only meaningful as a roll on a green.
        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.IRON))

        subject.show_round_result(straight_roll())

        assert floor.state.putt_path is None

    def test_gspro_calling_it_a_putt_is_enough_on_its_own(self) -> None:
        """`isPutt` was false on all 196 shots of the real capture, so CaddieJ's
        own answer is the more reliable of the two — but if GSPro ever does say
        so, that is not a reason to refuse."""

        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.IRON))
        rolled = straight_roll()

        subject.show_round_result(
            ObservedRoundShot(
                shot_id="putt",
                starting_position=rolled.starting_position,
                ending_position=rolled.ending_position,
                ball_path=rolled.ball_path,
                is_putt=True,
            )
        )

        assert floor.state.putt_path is not None

    def test_the_roll_reaches_the_browser_as_surface_points(self) -> None:
        from caddiej.adapters.hud.protocol import snapshot_message

        floor = FloorService()
        subject = a_presenter(floor)
        subject.show_putting_guide(Club(kind=ClubKind.PUTTER))
        subject.show_round_result(straight_roll())

        state = snapshot_message(floor.state).model_dump(mode="json")["state"]

        assert state["putt_path"]["source"] == "gspro_round_path"
        assert len(state["putt_path"]["points"]) >= 3
        assert state["putting_lane"]["visible"] is True
